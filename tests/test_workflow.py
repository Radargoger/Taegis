from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / "workflow.json"
PARAMETERS_PATH = ROOT / "parameters.example.json"
KQL_PATH = ROOT / "kql" / "queries.kql"


def walk_actions(
    actions: dict[str, object], ancestors: tuple[str, ...] = ()
):
    for name, raw in actions.items():
        if not isinstance(raw, dict):
            continue
        action_type = str(raw.get("type", ""))
        yield name, raw, ancestors
        nested = raw.get("actions")
        if isinstance(nested, dict):
            yield from walk_actions(nested, (*ancestors, action_type))
        alternate = raw.get("else")
        if isinstance(alternate, dict) and isinstance(alternate.get("actions"), dict):
            yield from walk_actions(alternate["actions"], (*ancestors, action_type))


class AzureWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
        cls.actions = list(walk_actions(cls.workflow["actions"]))

    def test_terminate_is_not_nested_in_foreach_or_until(self) -> None:
        for name, action, ancestors in self.actions:
            if action.get("type") == "Terminate":
                with self.subTest(action=name):
                    self.assertNotIn("Foreach", ancestors)
                    self.assertNotIn("Until", ancestors)

    def test_mutation_posts_have_no_automatic_retry(self) -> None:
        operations = {
            "createInvestigationV2",
            "updateInvestigationV2",
            "closeInvestigation",
        }
        found: set[str] = set()
        for name, action, _ in self.actions:
            inputs = action.get("inputs")
            if not isinstance(inputs, dict):
                continue
            body = inputs.get("body")
            if not isinstance(body, dict) or body.get("operationName") not in operations:
                continue
            found.add(str(body["operationName"]))
            with self.subTest(action=name):
                self.assertEqual(inputs.get("retryPolicy"), {"type": "none"})
        self.assertEqual(found, operations)

    def test_mutation_attempt_is_reserved_before_http(self) -> None:
        for name, action, _ in self.actions:
            inputs = action.get("inputs")
            if not isinstance(inputs, dict) or not isinstance(inputs.get("body"), dict):
                continue
            operation = inputs["body"].get("operationName")
            if operation not in {
                "createInvestigationV2",
                "updateInvestigationV2",
                "closeInvestigation",
            }:
                continue
            with self.subTest(action=name):
                dependencies = action.get("runAfter", {})
                self.assertEqual(len(dependencies), 1)
                self.assertTrue(next(iter(dependencies)).startswith("Increment_"))

    def test_secure_data_configuration_matches_supported_action_types(self) -> None:
        for name, action, _ in self.actions:
            secure = (
                action.get("runtimeConfiguration", {})
                .get("secureData", {})
                .get("properties")
            )
            if not secure:
                continue
            with self.subTest(action=name, action_type=action.get("type")):
                if action.get("type") == "Compose":
                    self.assertEqual(secure, ["inputs"])
                self.assertNotIn(action.get("type"), {"SetVariable", "Foreach"})

    def test_company_aware_correlation_and_apply_interlocks_are_present(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertNotIn("socradar-alarm-", text)
        self.assertIn("socradar-company-", text)
        self.assertIn("ApplyAlarmIds", text)
        self.assertIn("ApplyAllAlarms", text)
        self.assertIn("MaxMutationsPerRun", text)
        self.assertIn("mutation_count", text)
        self.assertNotIn("createdAt >=", text)

    def test_numeric_source_fields_are_normalized_before_empty(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertNotIn("empty(item()?['company_id'])", text)
        self.assertNotIn("empty(items('For_Each_Alarm')?['company_id'])", text)
        self.assertNotIn("empty(coalesce(item()?['alarm_id']", text)
        self.assertIn("isInt(string(coalesce(item()?['alarm_id']", text)
        self.assertIn("greater(int(string(coalesce(item()?['alarm_id']", text)

    def test_last_week_count_is_an_exact_timestamp_window(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("yyyy-MM-ddTHH:mm:ssZ", text)
        self.assertIn("parseDateTime", text)
        self.assertNotIn("ticks(addDays(outputs('Compose_End_Date'), 1))", text)

    def test_target_snapshot_and_case_ownership_fail_closed(self) -> None:
        text = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("Filter_Invalid_Taegis_Cases", text)
        self.assertIn("Compose_Unique_Taegis_Case_IDs", text)
        self.assertIn("Filter_Partial_Matching_Case", text)
        strict = self.workflow["actions"]["For_Each_Alarm"]["actions"][
            "Filter_Matching_Case"
        ]["inputs"]["where"]
        # Ownership authority is the deterministic tag alone: Taegis rewrites
        # serviceDeskType/serviceDeskId asynchronously (live-verified), so
        # they must not participate in matching.
        self.assertNotIn("serviceDesk", strict)
        self.assertIn("socradar-company-", strict)
        self.assertIn("length(split", strict)

    def test_pagination_handlers_observe_transport_failure_states(self) -> None:
        action_by_name = {name: action for name, action, _ in self.actions}
        for name in (
            "If_Open_Response_Failed",
            "If_OnHold_Response_Failed",
            "If_Closed_Response_Failed",
            "If_Taegis_Query_Has_Errors",
        ):
            with self.subTest(action=name):
                statuses = next(iter(action_by_name[name]["runAfter"].values()))
                self.assertEqual(set(statuses), {"Succeeded", "Failed", "TimedOut"})

    def test_source_pagination_metadata_is_mandatory(self) -> None:
        action_by_name = {name: action for name, action, _ in self.actions}
        for name in (
            "If_Open_Response_Failed",
            "If_OnHold_Response_Failed",
            "If_Closed_Response_Failed",
        ):
            expression = action_by_name[name]["expression"]
            with self.subTest(action=name):
                self.assertIn("total_pages", expression)
                self.assertIn("total_records", expression)
                self.assertIn("total_count", expression)
                self.assertIn("isInt", expression)

    def test_pagination_page_advance_failure_is_fatal(self) -> None:
        # Live incident (run 08584169888662533388882813644CU60, Jul 21):
        # Set_Open_Records hit VariableSizeLimitExceeded, the page increment
        # was skipped, and Until_Open_Pages_Complete spun on the same page for
        # 39 extra iterations because neither exit condition could ever become
        # true. Every pagination loop must (a) turn a broken advance chain into
        # fatal_error via a catch that fires when the increment does not
        # succeed, and (b) carry an absolute page cap in its exit condition.
        action_by_name = {name: action for name, action, _ in self.actions}
        loops = {
            "Until_Open_Pages_Complete": (
                "Increment_Open_Page",
                "Catch_Open_Page_Advance_Failed",
                "open_page",
            ),
            "Until_OnHold_Pages_Complete": (
                "Increment_OnHold_Page",
                "Catch_OnHold_Page_Advance_Failed",
                "onhold_page",
            ),
            "Until_Closed_Pages_Complete": (
                "Increment_Closed_Page",
                "Catch_Closed_Page_Advance_Failed",
                "closed_page",
            ),
            "Until_Taegis_Cases_Complete": (
                "Increment_Taegis_Page",
                "Catch_Taegis_Page_Advance_Failed",
                "taegis_page",
            ),
        }
        for loop_name, (increment, catch, page_var) in loops.items():
            with self.subTest(loop=loop_name):
                catch_action = action_by_name[catch]
                self.assertEqual(catch_action["type"], "SetVariable")
                self.assertEqual(catch_action["inputs"]["name"], "fatal_error")
                self.assertNotEqual(catch_action["inputs"]["value"], "")
                self.assertEqual(
                    catch_action["runAfter"],
                    {increment: ["Failed", "Skipped", "TimedOut"]},
                )
                self.assertIn(
                    f"greater(variables('{page_var}'), 200)",
                    action_by_name[loop_name]["expression"],
                )

    def test_unknown_closed_status_maps_to_inconclusive_fallback(self) -> None:
        compose = self.workflow["actions"]["For_Each_Alarm"]["actions"][
            "Compose_Close_Status"
        ]["inputs"]
        self.assertIn("CloseStatusFallback", compose)

    def test_existing_case_is_updated_before_close_when_content_changed(self) -> None:
        action_by_name = {name: action for name, action, _ in self.actions}
        update = action_by_name["Update_Taegis_Case_Before_Close"]
        close = action_by_name["If_Apply_Close"]
        self.assertEqual(
            update["runAfter"],
            {"Increment_Update_Before_Close_Mutation_Count": ["Succeeded"]},
        )
        self.assertEqual(
            close["runAfter"], {"If_Closed_Case_Needs_Update": ["Succeeded"]}
        )

    def test_mutation_responses_are_bound_to_requested_case_ownership(self) -> None:
        action_by_name = {name: action for name, action, _ in self.actions}
        create_check = action_by_name["If_Create_Has_Errors"]["expression"]
        update_check = action_by_name["If_Update_Has_Errors"]["expression"]
        close_check = action_by_name["If_Close_Has_Errors"]["expression"]
        self.assertNotIn("serviceDesk", create_check)
        self.assertIn("socradar-company-", create_check)
        self.assertNotIn("serviceDesk", update_check)
        self.assertIn("socradar-company-", update_check)
        self.assertIn("Compose_Existing_Case", close_check)

    def test_example_deployment_is_seamless_production(self) -> None:
        # Production-first install (product decision, 2026-07-22): deploy,
        # self-start ~3 minutes later, and write every in-scope alarm to
        # Taegis. syncMode=audit remains the opt-in dry run.
        parameters = json.loads(PARAMETERS_PATH.read_text(encoding="utf-8"))["parameters"]
        self.assertEqual(parameters["syncMode"]["value"], "apply")
        self.assertTrue(parameters["enableWorkflow"]["value"])
        self.assertFalse(parameters["backfillClosed"]["value"])
        self.assertEqual(parameters["applyAlarmIds"]["value"], [])
        self.assertTrue(parameters["applyAllAlarms"]["value"])
        self.assertEqual(parameters["maxMutationsPerRun"]["value"], 100)

    # ------------------------------------------------------------------
    # Helpers for the TOCTOU / ledger / policy contract tests.
    # ------------------------------------------------------------------

    def action(self, name: str) -> dict:
        for action_name, action, _ in self.actions:
            if action_name == name:
                return action
        raise AssertionError(f"action {name!r} not found in workflow")

    def subtree_action_names(self, action: dict) -> set[str]:
        names: set[str] = set()
        nested = action.get("actions")
        if isinstance(nested, dict):
            for child, raw in nested.items():
                names.add(child)
                names |= self.subtree_action_names(raw)
        alternate = action.get("else")
        if isinstance(alternate, dict) and isinstance(alternate.get("actions"), dict):
            for child, raw in alternate["actions"].items():
                names.add(child)
                names |= self.subtree_action_names(raw)
        return names

    def assert_is_fresh_case_read(self, name: str, expected_id_expression: str) -> None:
        read = self.action(name)
        self.assertEqual(read["type"], "Http")
        inputs = read["inputs"]
        self.assertEqual(inputs["method"], "POST")
        body = inputs["body"]
        self.assertEqual(body["operationName"], "investigationV2")
        self.assertIn("investigationV2(arguments: $arguments)", body["query"])
        self.assertEqual(
            body["variables"]["arguments"]["id"], expected_id_expression
        )
        self.assertEqual(inputs["retryPolicy"]["type"], "exponential")

    # (1) Exact pre-create search exists and gates the create correctly.
    def test_pre_create_search_is_exact_and_gates_create(self) -> None:
        search = self.action("Search_Case_Before_Create")
        self.assertEqual(search["type"], "Http")
        body = search["inputs"]["body"]
        self.assertEqual(body["operationName"], "investigationsV2")
        cql = body["variables"]["arguments"]["cql"]
        self.assertIn("tags = ''socradar-company-", cql)
        self.assertIn("outputs('Compose_Company_ID')", cql)
        self.assertIn("'-alarm-', outputs('Compose_Alarm_ID')", cql)
        self.assertEqual(search["inputs"]["retryPolicy"]["type"], "exponential")

        blocked = self.action("If_Pre_Create_Blocked")
        self.assertIn("Search_Case_Before_Create", blocked["expression"])
        self.assertIn("greater(int(coalesce(body('Search_Case_Before_Create')", blocked["expression"])
        self.assertIn("Append_Failed_Alarm_Pre_Create", self.subtree_action_names(blocked))

        do_create = self.action("If_Do_Create")
        expression = do_create["expression"]
        self.assertIn(
            "equals(actions('Search_Case_Before_Create')?['status'], 'Succeeded')",
            expression,
        )
        self.assertIn("empty(body('Search_Case_Before_Create')?['errors'])", expression)
        self.assertIn("investigations'], json('[1]'))), 0)", expression)
        self.assertIn("totalCount'], -1)), 0)", expression)
        self.assertIn("Create_Taegis_Case", self.subtree_action_names(do_create))
        create_scope = self.action("If_Apply_Create")
        self.assertIn("Search_Case_Before_Create", self.subtree_action_names(create_scope))

    # (2) Exact pre-update re-read exists and the update body uses fresh data.
    def test_pre_update_read_is_exact_and_fresh(self) -> None:
        self.assert_is_fresh_case_read(
            "Read_Case_Before_Update", "@outputs('Compose_Existing_Case')?['id']"
        )
        self.assert_is_fresh_case_read(
            "Read_Case_Before_Update_Before_Close",
            "@outputs('Compose_Existing_Case')?['id']",
        )
        update_input = self.action("Update_Taegis_Case")["inputs"]["body"]["variables"]["input"]
        self.assertEqual(
            update_input["id"],
            "@body('Read_Case_Before_Update')?['data']?['investigationV2']?['id']",
        )
        before_close_input = self.action("Update_Taegis_Case_Before_Close")["inputs"]["body"]["variables"]["input"]
        self.assertEqual(
            before_close_input["id"],
            "@body('Read_Case_Before_Update_Before_Close')?['data']?['investigationV2']?['id']",
        )
        self.assertIn(
            "body('Read_Case_Before_Update_Before_Close')?['data']?['investigationV2']?['status']",
            before_close_input["status"],
        )

    # (3) Exact pre-close re-read exists and the close body uses the fresh ID.
    def test_pre_close_read_is_exact_and_fresh(self) -> None:
        self.assert_is_fresh_case_read(
            "Read_Case_Before_Close", "@outputs('Compose_Existing_Case')?['id']"
        )
        self.assert_is_fresh_case_read(
            "Read_Case_Before_New_Close",
            "@body('Create_Taegis_Case')?['data']?['createInvestigationV2']?['id']",
        )
        close_input = self.action("Close_Existing_Taegis_Case")["inputs"]["body"]["variables"]["input"]
        self.assertEqual(
            close_input["id"],
            "@body('Read_Case_Before_Close')?['data']?['investigationV2']?['id']",
        )
        new_close_input = self.action("Close_New_Taegis_Case")["inputs"]["body"]["variables"]["input"]
        self.assertEqual(
            new_close_input["id"],
            "@body('Read_Case_Before_New_Close')?['data']?['investigationV2']?['id']",
        )

    # (4) A Case closed after the snapshot cancels the mutation with an audit event.
    def test_case_closed_after_snapshot_cancels_mutation(self) -> None:
        cancellations = {
            "If_Update_Cancelled_Case_Closed": "Read_Case_Before_Update",
            "If_Update_Before_Close_Cancelled_Case_Closed": "Read_Case_Before_Update_Before_Close",
            "If_Close_Cancelled_Case_Closed": "Read_Case_Before_Close",
            "If_New_Close_Cancelled_Case_Closed": "Read_Case_Before_New_Close",
        }
        for gate_name, read_name in cancellations.items():
            with self.subTest(gate=gate_name):
                gate = self.action(gate_name)
                expression = gate["expression"]
                self.assertIn(read_name, expression)
                self.assertIn("'CLOSED_'", expression)
                nested = gate["actions"]
                self.assertTrue(nested)
                for child in nested.values():
                    self.assertEqual(child["type"], "AppendToArrayVariable")
                cancel_text = json.dumps(nested)
                self.assertIn("cancelled_case_closed", cancel_text)
        for do_name in (
            "If_Do_Update",
            "If_Do_Update_Before_Close",
            "If_Do_Close",
            "If_Do_New_Close",
        ):
            with self.subTest(gate=do_name):
                self.assertIn(
                    "not(startsWith", self.action(do_name)["expression"]
                )

    # (5) Analyst tags added after the snapshot survive: tags come from the fresh read.
    def test_analyst_tags_added_after_snapshot_are_preserved_from_fresh_read(self) -> None:
        update_tags = self.action("Update_Taegis_Case")["inputs"]["body"]["variables"]["input"]["tags"]
        self.assertIn(
            "union(coalesce(body('Read_Case_Before_Update')?['data']?['investigationV2']?['tags']",
            update_tags,
        )
        self.assertNotIn("Compose_Existing_Case')?['tags']", update_tags)
        before_close_tags = self.action("Update_Taegis_Case_Before_Close")["inputs"]["body"]["variables"]["input"]["tags"]
        self.assertIn(
            "union(coalesce(body('Read_Case_Before_Update_Before_Close')?['data']?['investigationV2']?['tags']",
            before_close_tags,
        )
        self.assertNotIn("Compose_Existing_Case')?['tags']", before_close_tags)
        for tags in (update_tags, before_close_tags):
            self.assertIn("createArray('SOCRadar'", tags)
            self.assertIn("socradar-company-", tags)

    # (6) Conflicting ownership is rejected and recorded as a per-alarm failure.
    def test_conflicting_ownership_is_rejected_per_alarm(self) -> None:
        duplicate = self.action("If_Duplicate_Mapping")
        self.assertIn(
            "Append_Failed_Alarm_Duplicate_Mapping", self.subtree_action_names(duplicate)
        )
        conflicts = {
            "If_Pre_Update_Conflict": "Read_Case_Before_Update",
            "If_Pre_Update_Before_Close_Conflict": "Read_Case_Before_Update_Before_Close",
            "If_Pre_Close_Conflict": "Read_Case_Before_Close",
            "If_Pre_New_Close_Conflict": "Read_Case_Before_New_Close",
        }
        for gate_name, read_name in conflicts.items():
            with self.subTest(gate=gate_name):
                gate = self.action(gate_name)
                expression = gate["expression"]
                self.assertIn(read_name, expression)
                self.assertNotIn("serviceDesk", expression)
                self.assertIn("socradar-company-", expression)
                nested_text = json.dumps(gate["actions"])
                self.assertIn("failed_alarm_ids", nested_text)

    # (7) A mutation response bound to the wrong Case ID is a failure.
    def test_mutation_response_case_id_mismatch_is_failure(self) -> None:
        create_check = self.action("If_Create_Has_Errors")["expression"]
        self.assertIn(
            "empty(body('Create_Taegis_Case')?['data']?['createInvestigationV2']?['id'])",
            create_check,
        )
        update_check = self.action("If_Update_Has_Errors")["expression"]
        self.assertIn(
            "not(equals(body('Update_Taegis_Case')?['data']?['updateInvestigationV2']?['id'], outputs('Compose_Existing_Case')?['id']))",
            update_check,
        )
        before_close_check = self.action("If_Update_Before_Close_Has_Errors")["expression"]
        self.assertIn(
            "not(equals(body('Update_Taegis_Case_Before_Close')?['data']?['updateInvestigationV2']?['id'], outputs('Compose_Existing_Case')?['id']))",
            before_close_check,
        )
        close_check = self.action("If_Close_Has_Errors")["expression"]
        self.assertIn(
            "not(equals(body('Close_Existing_Taegis_Case')?['data']?['closeInvestigation']?['id'], outputs('Compose_Existing_Case')?['id']))",
            close_check,
        )
        new_close_check = self.action("If_New_Close_Has_Errors")["expression"]
        self.assertIn(
            "not(equals(body('Close_New_Taegis_Case')?['data']?['closeInvestigation']?['id'], body('Create_Taegis_Case')?['data']?['createInvestigationV2']?['id']))",
            new_close_check,
        )
        for gate in (
            "If_Update_Has_Errors",
            "If_Update_Before_Close_Has_Errors",
            "If_Close_Has_Errors",
            "If_New_Close_Has_Errors",
            "If_Create_Has_Errors",
        ):
            with self.subTest(gate=gate):
                self.assertIn("failed_alarm_ids", json.dumps(self.action(gate)["actions"]))

    # (8) Every successful mutation adopts a durable source fingerprint in the ledger.
    def test_successful_mutations_adopt_durable_fingerprint_in_ledger(self) -> None:
        parameters = self.workflow["parameters"]
        for name in (
            "LedgerTableEndpoint",
            "LedgerMappingTableName",
            "LedgerEventsTableName",
            "LedgerRetentionDays",
        ):
            self.assertIn(name, parameters)
        fingerprint = self.action("Compose_Source_Fingerprint")["inputs"]
        for source_field in (
            "Compose_Case_Title",
            "Compose_Case_Priority",
            "Compose_Source_Status",
            "Compose_Key_Findings",
        ):
            self.assertIn(source_field, fingerprint)
        writes = (
            "Write_Ledger_Mapping_Created",
            "Write_Ledger_Mapping_New_Close",
            "Write_Ledger_Mapping_Updated_Before_Close",
            "Write_Ledger_Mapping_Closed",
            "Write_Ledger_Mapping_Updated",
        )
        for name in writes:
            with self.subTest(write=name):
                write = self.action(name)
                inputs = write["inputs"]
                self.assertEqual(inputs["method"], "PUT")
                self.assertEqual(
                    inputs["authentication"],
                    {"type": "ManagedServiceIdentity", "audience": "https://storage.azure.com/"},
                )
                self.assertIn("LedgerMappingTableName", inputs["uri"])
                self.assertIn("outputs('Compose_Company_ID')", inputs["uri"])
                self.assertIn("outputs('Compose_Alarm_ID')", inputs["uri"])
                body = inputs["body"]
                self.assertEqual(body["SourceFingerprint"], "@outputs('Compose_Source_Fingerprint')")
                for field in ("CaseId", "SourceStatus", "TargetStatus", "LastSuccessTimeUtc", "TenantBinding"):
                    self.assertIn(field, body)
                self.assertEqual(inputs["retryPolicy"]["type"], "exponential")
        # A permanently failed ledger write marks the alarm failed (fail-closed evidence).
        text = WORKFLOW_PATH.read_text(encoding="utf-8")
        handlers = {
            name: action.get("runAfter", {})
            for name, action, _ in self.actions
        }
        for name in writes:
            with self.subTest(handler_for=name):
                matches = [
                    h for h, run_after in handlers.items()
                    if set(run_after.get(name, [])) == {"Failed", "TimedOut"}
                ]
                self.assertEqual(len(matches), 1, matches)

    # (9) Post-close drift is persisted to the durable events ledger.
    def test_post_close_drift_is_persisted_to_ledger(self) -> None:
        drift_gate = self.action("If_Closed_Case_Drifted")
        nested = self.subtree_action_names(drift_gate)
        self.assertIn("Append_Drift_After_Close", nested)
        self.assertIn("Write_Ledger_Drift_Event", nested)
        self.assertIn("Append_Failed_Alarm_Ledger_Drift", nested)
        write = self.action("Write_Ledger_Drift_Event")
        inputs = write["inputs"]
        self.assertEqual(inputs["method"], "PUT")
        self.assertIn("LedgerEventsTableName", inputs["uri"])
        self.assertIn("workflow()?['run']?['name']", inputs["uri"])
        self.assertEqual(inputs["body"]["EventType"], "drift_after_close")
        self.assertEqual(
            inputs["authentication"],
            {"type": "ManagedServiceIdentity", "audience": "https://storage.azure.com/"},
        )

    # (10) A ledger tenant/company binding mismatch terminates the run up front.
    def test_ledger_binding_mismatch_terminates_run(self) -> None:
        binding = self.action("Compose_Expected_Binding")["inputs"]
        for parameter in ("TaegisBaseUrl", "TaegisTenantId", "SocradarCompanyId"):
            self.assertIn(parameter, binding)
        read = self.action("Read_Ledger_Binding")
        self.assertEqual(read["inputs"]["method"], "GET")
        self.assertIn("__binding__", read["inputs"]["uri"])
        self.assertIn("identity", read["inputs"]["uri"])
        gate = self.action("If_Ledger_Binding_Mismatch")
        self.assertIn("Compose_Expected_Binding", gate["expression"])
        self.assertIn("404", gate["expression"])
        gate_text = json.dumps(gate["actions"])
        self.assertIn("Terminate", gate_text)
        self.assertIn("LedgerBindingMismatch", gate_text)
        else_text = json.dumps(gate["else"]["actions"])
        self.assertIn("Write_Ledger_Binding", else_text)
        self.assertIn("LedgerWriteFailed", else_text)
        # Binding is verified before any state is read or any alarm is processed.
        # Initialize_Run_State was split into a single-variable chain
        # (Logic Apps allows one variable per InitializeVariable action);
        # the chain head must still gate on the binding check.
        chain_heads = [
            name
            for name, action, _ in self.actions
            if action.get("type") == "InitializeVariable"
            and action.get("runAfter") == {"If_Ledger_Binding_Mismatch": ["Succeeded"]}
        ]
        self.assertEqual(len(chain_heads), 1, chain_heads)

    # (11) An incomplete source or target snapshot means zero mutations.
    def test_incomplete_snapshot_means_zero_mutation(self) -> None:
        gate = self.action("If_Source_Snapshot_Invalid")
        self.assertIn(
            "Terminate_Invalid_Source_Snapshot", self.subtree_action_names(gate)
        )
        expression = gate["expression"]
        self.assertIn("Filter_Invalid_Taegis_Cases", expression)
        self.assertIn("Filter_Invalid_Source_Records", expression)
        self.assertIn("Compose_Unique_Open_View_Keys", expression)
        self.assertIn("Compose_Unique_OnHold_View_Keys", expression)
        self.assertIn("Compose_Unique_Closed_View_Keys", expression)
        self.assertIn("variables('taegis_total')", expression)
        # Mutations live only inside For_Each_Alarm, which is gated on the snapshot check.
        self.assertEqual(
            self.action("For_Each_Alarm")["runAfter"],
            {"If_Source_Snapshot_Invalid": ["Succeeded"]},
        )
        # Snapshot-level read failures terminate before the loop as well.
        for name in (
            "If_Fatal_After_Open",
            "If_Fatal_After_OnHold",
            "If_Fatal_After_Closed",
            "If_Fatal_After_Taegis_Query",
            "If_Token_Missing",
        ):
            with self.subTest(gate=name):
                self.assertIn("Terminate", json.dumps(self.action(name)["actions"]))

    # Policy P1: a cross-view transitional duplicate is not fatal; resolved wins.
    # The unresolved side is the OPEN + ON_HOLD union; resolved is the CLOSED view.
    def test_policy_p1_resolved_view_wins_cross_view_duplicates(self) -> None:
        overlap = self.action("Compose_Cross_View_Overlap")["inputs"]
        self.assertIn("intersection", overlap)
        self.assertIn("Compose_Unique_Unresolved_View_Keys", overlap)
        self.assertIn("Compose_Unique_Closed_View_Keys", overlap)
        transition = self.action("If_Cross_View_Transition")
        self.assertIn(
            "cross_view_transition_resolved_wins", json.dumps(transition["actions"])
        )
        merge = self.action("Compose_Resolved_Wins_Source")["inputs"]
        self.assertIn("body('Filter_Open_Not_Resolved')", merge)
        self.assertIn("variables('closed_records')", merge)
        keep_open = self.action("Filter_Open_Not_Resolved")["inputs"]
        self.assertEqual(keep_open["from"], "@outputs('Compose_Unresolved_Records')")
        self.assertIn("Compose_Unique_Closed_View_Keys", keep_open["where"])
        unresolved = self.action("Compose_Unresolved_Records")["inputs"]
        self.assertIn("variables('open_records')", unresolved)
        self.assertIn("variables('onhold_records')", unresolved)
        # The old combined total check (which failed on clean transitions) is gone;
        # per-view consistency stays fatal.
        snapshot_expression = self.action("If_Source_Snapshot_Invalid")["expression"]
        self.assertNotIn(
            "add(variables('open_api_total'), variables('closed_api_total'))",
            snapshot_expression,
        )
        self.assertIn("Select_Open_View_Keys", snapshot_expression)
        self.assertIn("Select_OnHold_View_Keys", snapshot_expression)
        self.assertIn("Select_Closed_View_Keys", snapshot_expression)

    # Policy P2: mutation failures are isolated per alarm; the run fails at the end.
    def test_policy_p2_per_alarm_failure_isolation(self) -> None:
        for gate_name in (
            "If_Apply_Create",
            "If_Apply_Update_Before_Close",
            "If_Apply_Close",
            "If_Apply_Update",
        ):
            with self.subTest(gate=gate_name):
                expression = self.action(gate_name)["expression"]
                self.assertNotIn("fatal_error", expression)
                self.assertIn(
                    "not(contains(variables('failed_alarm_ids'), outputs('Compose_Alarm_ID')))",
                    expression,
                )
                self.assertIn("MaxMutationsPerRun", expression)
        # Nothing inside the loop writes the run-level fatal flag any more.
        loop_text = json.dumps(self.action("For_Each_Alarm"))
        self.assertNotIn('"name": "fatal_error"', loop_text)
        self.assertIn('"name": "failed_alarm_ids"', loop_text)
        final_gate = self.action("If_Alarm_Processing_Failed")["expression"]
        self.assertIn("failed_alarm_ids", final_gate)

    def test_run_summary_uses_precise_occurrence_field_names(self) -> None:
        summary = self.workflow["actions"]["Compose_Run_Summary"]["inputs"]
        self.assertNotIn("last_week_total", summary)
        self.assertEqual(
            summary["alarms_occurred_in_lookback_window"],
            "@length(body('Filter_Source_Last_Week'))",
        )
        self.assertEqual(
            summary["alarms_occurred_last_7_days"],
            "@length(body('Filter_Source_Trailing_7_Days'))",
        )
        for live_total in (
            "open_api_total",
            "onhold_api_total",
            "closed_api_total",
            "source_total",
            "taegis_case_total",
            "mutation_count",
            "failed_alarm_total",
        ):
            self.assertIn(live_total, summary)
        trailing = self.action("Filter_Source_Trailing_7_Days")["inputs"]["where"]
        self.assertIn("addDays(utcNow(), -7)", trailing)
        self.assertIn("item()?['date']", trailing)
        # The run summary is persisted before the run is allowed to fail.
        self.assertEqual(
            self.action("Write_Ledger_Run_Summary")["runAfter"],
            {"Compose_Run_Summary": ["Succeeded"]},
        )
        self.assertIn(
            "LedgerWriteFailed",
            json.dumps(self.action("If_Run_Summary_Ledger_Failed")["actions"]),
        )
        self.assertEqual(
            self.action("If_Alarm_Processing_Failed")["runAfter"],
            {"If_Run_Summary_Ledger_Failed": ["Succeeded"]},
        )

    def test_kql_queries_match_example_workflow_name(self) -> None:
        kql = KQL_PATH.read_text(encoding="utf-8")
        parameters = json.loads(PARAMETERS_PATH.read_text(encoding="utf-8"))["parameters"]
        deployed_name = parameters["workflowName"]["value"]
        self.assertIn(f'Resource =~ "{deployed_name}"', kql)
        self.assertIn("replace", kql.lower())
        self.assertIn("workflowName", kql)

    # ------------------------------------------------------------------
    # Layered scheduling, epoch windows, and business-event telemetry.
    # ------------------------------------------------------------------

    def test_layered_scheduling_parameters_and_trigger_source(self) -> None:
        parameters = self.workflow["parameters"]
        for name in (
            "RecurrenceIntervalMinutes",
            "OverlapMinutes",
            "InitialLookbackDays",
            "ReconcileHourUtc",
            "ReconcileLookbackDays",
            "ScanStrategy",
            "LogIngestionEndpoint",
            "DcrImmutableId",
            "StreamName",
        ):
            with self.subTest(parameter=name):
                self.assertIn(name, parameters)
        self.assertNotIn("PollingIntervalMinutes", parameters)
        recurrence = self.workflow["triggers"]["Recurrence"]["recurrence"]
        self.assertEqual(recurrence["frequency"], "Minute")
        self.assertEqual(
            recurrence["interval"], "@parameters('RecurrenceIntervalMinutes')"
        )
        bicep = (ROOT / "main.bicep").read_text(encoding="utf-8")
        for line in (
            "param recurrenceIntervalMinutes int = 15",
            "param overlapMinutes int = 10",
            "param initialLookbackDays int = 1",
            "param reconcileHourUtc int = 6",
            "param reconcileLookbackDays int = 7",
            "param scanStrategy string = 'layered'",
            "'layered'",
        ):
            with self.subTest(bicep_line=line):
                self.assertIn(line, bicep)
        self.assertNotIn("pollingIntervalMinutes", bicep)

    def test_schedule_marker_read_write_and_failure_contract(self) -> None:
        read = self.action("Read_Ledger_Schedule")
        self.assertEqual(read["inputs"]["method"], "GET")
        self.assertIn("__schedule__", read["inputs"]["uri"])
        self.assertIn("RowKey=''state''", read["inputs"]["uri"])
        self.assertIn("LedgerMappingTableName", read["inputs"]["uri"])
        self.assertEqual(
            read["inputs"]["authentication"],
            {"type": "ManagedServiceIdentity", "audience": "https://storage.azure.com/"},
        )
        self.assertEqual(read["runAfter"], {"If_Apply_Scope_Invalid": ["Succeeded"]})
        # A non-404 read failure refuses to pick a window blindly (layered only).
        gate = self.action("If_Schedule_Read_Failed")
        self.assertIn("'layered'", gate["expression"])
        self.assertIn("404", gate["expression"])
        self.assertIn("Read_Ledger_Schedule", gate["expression"])
        self.assertIn("Terminate_Schedule_Read_Failed", self.subtree_action_names(gate))
        # Binding gate ordering is untouched; window selection happens before it.
        self.assertEqual(
            self.action("Compose_Expected_Binding")["runAfter"],
            {"Compose_Window_Start_Epoch": ["Succeeded"]},
        )
        # The marker is written only after the final failure gate: a failed run
        # terminates inside If_Alarm_Processing_Failed and never updates it.
        marker_gate = self.action("If_Update_Schedule_Marker")
        self.assertEqual(
            marker_gate["runAfter"], {"If_Alarm_Processing_Failed": ["Succeeded"]}
        )
        self.assertIn("'layered'", marker_gate["expression"])
        self.assertIn("'initial'", marker_gate["expression"])
        self.assertIn("'reconcile'", marker_gate["expression"])
        self.assertNotIn("'incremental'", marker_gate["expression"])
        write = self.action("Write_Ledger_Schedule_Marker")
        self.assertEqual(write["inputs"]["method"], "PUT")
        self.assertIn("__schedule__", write["inputs"]["uri"])
        self.assertIn("RowKey=''state''", write["inputs"]["uri"])
        body = write["inputs"]["body"]
        self.assertIn(
            "coalesce(body('Read_Ledger_Schedule')?['FirstRunDoneUtc']",
            body["FirstRunDoneUtc"],
        )
        self.assertIn(
            "if(equals(outputs('Compose_Window_Mode'), 'reconcile'), formatDateTime(utcNow(), 'yyyy-MM-dd')",
            body["LastReconcileDateUtc"],
        )
        self.assertIn(
            "coalesce(body('Read_Ledger_Schedule')?['LastReconcileDateUtc']",
            body["LastReconcileDateUtc"],
        )
        self.assertEqual(write["inputs"]["retryPolicy"]["type"], "exponential")
        failure = self.action("If_Schedule_Marker_Write_Failed")
        self.assertIn("Write_Ledger_Schedule_Marker", failure["expression"])
        self.assertIn(
            "Terminate_Schedule_Marker_Write_Failed", self.subtree_action_names(failure)
        )
        for name, _, ancestors in self.actions:
            if name in ("Read_Ledger_Schedule", "Write_Ledger_Schedule_Marker"):
                with self.subTest(action=name):
                    self.assertNotIn("Foreach", ancestors)
                    self.assertNotIn("Until", ancestors)

    def test_epoch_second_windows_and_window_mode_selection(self) -> None:
        end = self.action("Compose_Window_End_Epoch")["inputs"]
        self.assertEqual(
            end,
            "@div(sub(ticks(utcNow()), ticks('1970-01-01T00:00:00Z')), 10000000)",
        )
        start = self.action("Compose_Window_Start_Epoch")["inputs"]
        self.assertIn("mul(parameters('InitialLookbackDays'), 86400)", start)
        self.assertIn("mul(parameters('ReconcileLookbackDays'), 86400)", start)
        self.assertIn(
            "mul(add(parameters('RecurrenceIntervalMinutes'), parameters('OverlapMinutes')), 60)",
            start,
        )
        mode = self.action("Compose_Window_Mode")["inputs"]
        for token in (
            "'layered'",
            "'initial'",
            "'reconcile'",
            "'incremental'",
            "'full'",
            "parameters('ReconcileHourUtc')",
            "LastReconcileDateUtc",
            "formatDateTime(utcNow(), 'yyyy-MM-dd')",
        ):
            with self.subTest(token=token):
                self.assertIn(token, mode)
        for page in (
            "Get_SOCRadar_Open_Page",
            "Get_SOCRadar_OnHold_Page",
            "Get_SOCRadar_Closed_Page",
        ):
            queries = self.action(page)["inputs"]["queries"]
            with self.subTest(page=page):
                self.assertEqual(
                    queries["start_date"],
                    "@string(outputs('Compose_Window_Start_Epoch'))",
                )
                self.assertEqual(
                    queries["end_date"],
                    "@string(outputs('Compose_Window_End_Epoch'))",
                )
        summary = self.workflow["actions"]["Compose_Run_Summary"]["inputs"]
        self.assertEqual(summary["window_mode"], "@outputs('Compose_Window_Mode')")
        self.assertEqual(
            summary["window_start_epoch"], "@outputs('Compose_Window_Start_Epoch')"
        )
        self.assertEqual(
            summary["window_end_epoch"], "@outputs('Compose_Window_End_Epoch')"
        )
        self.assertIn("scan_strategy", summary)

    def test_layered_is_default_and_full_stays_regression_safe(self) -> None:
        # Product decision (2026-07-21): layered is the default operating
        # mode; full remains the completeness fallback and its window
        # contract below must not regress.
        parameters = json.loads(PARAMETERS_PATH.read_text(encoding="utf-8"))["parameters"]
        self.assertEqual(parameters["scanStrategy"]["value"], "layered")
        self.assertEqual(parameters["recurrenceIntervalMinutes"]["value"], 15)
        self.assertEqual(parameters["overlapMinutes"]["value"], 10)
        self.assertEqual(parameters["initialLookbackDays"]["value"], 1)
        self.assertEqual(parameters["reconcileHourUtc"]["value"], 6)
        self.assertEqual(parameters["reconcileLookbackDays"]["value"], 7)
        self.assertEqual(parameters["logAnalyticsWorkspaceResourceId"]["value"], "")
        # Every SOCRadar call carries an explicit epoch window. In full mode the
        # window resolves to start_date=0 .. end_date=now: a dateless request
        # silently degrades to the API's ~30-day default window (live-verified),
        # so the legacy dateless full-scan URI is forbidden.
        for page, status in (
            ("Get_SOCRadar_Open_Page", "OPEN"),
            ("Get_SOCRadar_OnHold_Page", "ON_HOLD"),
            ("Get_SOCRadar_Closed_Page", "CLOSED"),
        ):
            inputs = self.action(page)["inputs"]
            page_variable = {
                "OPEN": "open_page",
                "ON_HOLD": "onhold_page",
                "CLOSED": "closed_page",
            }[status]
            with self.subTest(page=page):
                self.assertNotIn("Compose_Window_Mode", inputs["uri"])
                self.assertNotIn("start_date", inputs["uri"])
                self.assertEqual(
                    inputs["queries"],
                    {
                        "page": f"@variables('{page_variable}')",
                        "limit": "100",
                        "include_total_records": "true",
                        "status": status,
                        "start_date": "@string(outputs('Compose_Window_Start_Epoch'))",
                        "end_date": "@string(outputs('Compose_Window_End_Epoch'))",
                    },
                )
        # Full strategy resolves the window mode to 'full' without the ledger
        # marker, and the full window start is the all-time epoch 0.
        mode = self.action("Compose_Window_Mode")["inputs"]
        self.assertTrue(mode.endswith("'full')"))
        start = self.action("Compose_Window_Start_Epoch")["inputs"]
        self.assertTrue(start.rstrip().endswith(", 0)))"))
        summary = self.workflow["actions"]["Compose_Run_Summary"]["inputs"]
        self.assertEqual(
            summary["source_scan_mode"],
            "@if(equals(outputs('Compose_Window_Mode'), 'full'), 'full', 'windowed')",
        )

    # ------------------------------------------------------------------
    # Live-verified SOCRadar view semantics: single-valued status views,
    # the ON_HOLD pagination loop, explicit epoch windows on every call,
    # and the three-way snapshot guard.
    # ------------------------------------------------------------------

    def test_socradar_views_use_status_parameter_not_is_resolved(self) -> None:
        # Live probe: incidents/v4 silently ignores is_resolved; the real filter
        # is the single-valued status parameter (comma lists return 500).
        text = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertNotIn("is_resolved", text)
        expected = {
            "Get_SOCRadar_Open_Page": "OPEN",
            "Get_SOCRadar_OnHold_Page": "ON_HOLD",
            "Get_SOCRadar_Closed_Page": "CLOSED",
        }
        for page, status in expected.items():
            with self.subTest(page=page):
                queries = self.action(page)["inputs"]["queries"]
                self.assertEqual(queries["status"], status)
                self.assertNotIn(",", queries["status"])

    def test_onhold_pagination_loop_mirrors_open_and_closed(self) -> None:
        loop = self.action("Until_OnHold_Pages_Complete")
        self.assertEqual(loop["type"], "Until")
        self.assertIn("onhold_page", loop["expression"])
        self.assertIn("onhold_total_pages", loop["expression"])
        self.assertIn("fatal_error", loop["expression"])
        self.assertEqual(loop["limit"], {"count": 1000, "timeout": "PT1H"})
        # OPEN -> ON_HOLD -> CLOSED, each behind the previous fatal gate.
        self.assertEqual(loop["runAfter"], {"If_Fatal_After_Open": ["Succeeded"]})
        self.assertEqual(
            self.action("If_Fatal_After_OnHold")["runAfter"],
            {"Until_OnHold_Pages_Complete": ["Succeeded"]},
        )
        self.assertEqual(
            self.action("Until_Closed_Pages_Complete")["runAfter"],
            {"If_Fatal_After_OnHold": ["Succeeded"]},
        )
        get = self.action("Get_SOCRadar_OnHold_Page")
        self.assertEqual(get["inputs"]["method"], "GET")
        self.assertEqual(get["inputs"]["retryPolicy"]["type"], "exponential")
        self.assertEqual(
            get["runtimeConfiguration"]["secureData"]["properties"],
            ["inputs", "outputs"],
        )
        failed = self.action("If_OnHold_Response_Failed")
        self.assertIn("is_success", failed["expression"])
        self.assertIn("SOCRadarOnHoldFailed", json.dumps(failed["actions"]))
        self.assertIn(
            "SOCRadarOnHoldFailed",
            json.dumps(self.action("If_Fatal_After_OnHold")["actions"]),
        )
        # The loop keeps its own records merge, api total, and page increment.
        for name in (
            "Compose_OnHold_Records",
            "Compose_Merged_OnHold_Records",
            "Set_OnHold_Records",
            "Set_OnHold_API_Total",
            "Set_OnHold_Total_Pages",
            "Increment_OnHold_Page",
        ):
            with self.subTest(action=name):
                self.action(name)
        self.assertIn(
            "onhold_api_total", json.dumps(self.action("Set_OnHold_API_Total"))
        )
        for name, _, ancestors in self.actions:
            if name == "Get_SOCRadar_OnHold_Page":
                self.assertIn("Until", ancestors)
        # ON_HOLD records land on the unresolved side of the reconciliation.
        unresolved = self.action("Compose_Unresolved_Records")["inputs"]
        self.assertIn("variables('onhold_records')", unresolved)
        # ON_HOLD alarms report status INVESTIGATING, mapped to an ACTIVE Case.
        target = self.action("Compose_Target_Open_Status")["inputs"]
        self.assertIn("'INVESTIGATING'", target)
        self.assertIn("'ACTIVE'", target)

    def test_every_socradar_get_carries_an_explicit_epoch_window(self) -> None:
        socradar_gets = []
        for name, action, _ in self.actions:
            inputs = action.get("inputs")
            if (
                isinstance(inputs, dict)
                and inputs.get("method") == "GET"
                and "incidents/v4" in str(inputs.get("uri", ""))
            ):
                socradar_gets.append((name, inputs))
        self.assertEqual(
            sorted(name for name, _ in socradar_gets),
            [
                "Get_SOCRadar_Closed_Page",
                "Get_SOCRadar_OnHold_Page",
                "Get_SOCRadar_Open_Page",
            ],
        )
        for name, inputs in socradar_gets:
            with self.subTest(action=name):
                queries = inputs["queries"]
                self.assertEqual(
                    queries["start_date"],
                    "@string(outputs('Compose_Window_Start_Epoch'))",
                )
                self.assertEqual(
                    queries["end_date"],
                    "@string(outputs('Compose_Window_End_Epoch'))",
                )
                self.assertIn("status", queries)
                self.assertIn("page", queries)
                self.assertIn("limit", queries)
                # No conditional dateless URI branch remains.
                self.assertNotIn("start_date", inputs["uri"])
                self.assertNotIn("Compose_Window_Mode", inputs["uri"])

    def test_snapshot_guard_validates_three_view_totals_and_onhold_isolation(self) -> None:
        overlap = self.action("Compose_Open_OnHold_Overlap")["inputs"]
        self.assertIn("intersection", overlap)
        self.assertIn("Compose_Unique_Open_View_Keys", overlap)
        self.assertIn("Compose_Unique_OnHold_View_Keys", overlap)
        unresolved_keys = self.action("Compose_Unique_Unresolved_View_Keys")["inputs"]
        self.assertIn("union", unresolved_keys)
        self.assertIn("Compose_Unique_Open_View_Keys", unresolved_keys)
        self.assertIn("Compose_Unique_OnHold_View_Keys", unresolved_keys)
        expression = self.action("If_Source_Snapshot_Invalid")["expression"]
        # The same alarm in both live views (OPEN and ON_HOLD) is corruption:
        # fail closed, zero mutations.
        self.assertIn("not(empty(outputs('Compose_Open_OnHold_Overlap')))", expression)
        # Three independent api totals, one per status view (D4):
        # unresolved total == OPEN total + ON_HOLD total holds because the
        # intersection above must be empty.
        for total, keys in (
            ("open_api_total", "Compose_Unique_Open_View_Keys"),
            ("onhold_api_total", "Compose_Unique_OnHold_View_Keys"),
            ("closed_api_total", "Compose_Unique_Closed_View_Keys"),
        ):
            with self.subTest(total=total):
                self.assertIn(
                    f"and(greaterOrEquals(variables('{total}'), 0), "
                    f"not(equals(variables('{total}'), length(outputs('{keys}')))))",
                    expression,
                )
        # In-view duplicates stay fatal for all three views.
        for select, unique in (
            ("Select_Open_View_Keys", "Compose_Unique_Open_View_Keys"),
            ("Select_OnHold_View_Keys", "Compose_Unique_OnHold_View_Keys"),
            ("Select_Closed_View_Keys", "Compose_Unique_Closed_View_Keys"),
        ):
            with self.subTest(select=select):
                self.assertIn(
                    f"not(equals(length(body('{select}')), length(outputs('{unique}'))))",
                    expression,
                )
        # The ON_HOLD key projection uses the same company-aware key shape.
        select_onhold = self.action("Select_OnHold_View_Keys")["inputs"]
        self.assertEqual(select_onhold["from"], "@variables('onhold_records')")
        self.assertIn("company_id", select_onhold["select"])
        self.assertIn("alarm_id", select_onhold["select"])

    def test_business_events_bicep_dce_dcr_table_and_role_scope(self) -> None:
        bicep = (ROOT / "main.bicep").read_text(encoding="utf-8")
        for token in (
            "Microsoft.Insights/dataCollectionEndpoints@",
            "Microsoft.Insights/dataCollectionRules@",
            "'Custom-SOCRadarTaegisBridge_CL'",
            "3913510d-42f4-4e42-8a64-420c390055eb",
            "scope: businessEventsDcr",
            "modules/law-table.bicep",
            "resourceGroup(lawSubscriptionId, lawResourceGroupName)",
            "businessEventsEnabled ? businessEventsDce!.properties.logsIngestion.endpoint : ''",
            "businessEventsEnabled ? businessEventsDcr!.properties.immutableId : ''",
            "businessEventsEnabled ? businessEventsStreamName : ''",
        ):
            with self.subTest(token=token):
                self.assertIn(token, bicep)
        # The publisher role is granted on the DCR, never on the DCE.
        self.assertNotIn("scope: businessEventsDce", bicep)
        module = (
            ROOT / "modules" / "law-table.bicep"
        ).read_text(encoding="utf-8")
        self.assertIn("existing", module)
        self.assertIn("SOCRadarTaegisBridge_CL", module)
        for column in (
            "TimeGenerated",
            "RunId",
            "WindowMode",
            "CompanyId",
            "AlarmId",
            "EventType",
            "CaseId",
            "Detail",
        ):
            with self.subTest(column=column):
                self.assertIn(f"name: '{column}'", module)

    def test_business_events_workflow_post_is_optional_and_isolated(self) -> None:
        # Telemetry lives in the success branch of the run-summary ledger gate:
        # it runs only after the durable evidence write, before the final
        # failure terminate, without touching any pinned runAfter contract.
        ledger_gate = self.action("If_Run_Summary_Ledger_Failed")
        self.assertIn("If_Send_Business_Events", self.subtree_action_names(ledger_gate))
        send_gate = self.action("If_Send_Business_Events")
        for parameter in ("LogIngestionEndpoint", "DcrImmutableId", "StreamName"):
            with self.subTest(parameter=parameter):
                self.assertIn(
                    f"not(empty(parameters('{parameter}')))", send_gate["expression"]
                )
        post = self.action("Post_Business_Events")
        self.assertEqual(post["inputs"]["method"], "POST")
        self.assertEqual(
            post["inputs"]["authentication"],
            {"type": "ManagedServiceIdentity", "audience": "https://monitor.azure.com"},
        )
        self.assertEqual(post["inputs"]["retryPolicy"]["type"], "exponential")
        for fragment in ("/dataCollectionRules/", "/streams/", "api-version="):
            self.assertIn(fragment, post["inputs"]["uri"])
        self.assertEqual(post["inputs"]["body"], "@body('Select_Business_Events')")
        # Telemetry is not a mutation: it is never reserved by a mutation-count
        # increment and never inside the per-alarm loop.
        self.assertEqual(post["runAfter"], {"Select_Business_Events": ["Succeeded"]})
        for name, _, ancestors in self.actions:
            if name == "Post_Business_Events":
                self.assertNotIn("Foreach", ancestors)
                self.assertNotIn("Until", ancestors)
        select = self.action("Select_Business_Events")["inputs"]["select"]
        for column in (
            "TimeGenerated",
            "RunId",
            "WindowMode",
            "CompanyId",
            "AlarmId",
            "EventType",
            "CaseId",
            "Detail",
        ):
            with self.subTest(column=column):
                self.assertIn(column, select)
        # One batch: audit events plus exactly one run_summary row.
        summary_row = self.action("Append_Run_Summary_Business_Event")
        self.assertEqual(summary_row["inputs"]["value"]["action"], "run_summary")
        # A telemetry failure is observed and noted but never fails the run.
        failed_gate = self.action("If_Business_Events_Post_Failed")
        statuses = next(iter(failed_gate["runAfter"].values()))
        self.assertEqual(set(statuses), {"Succeeded", "Failed", "TimedOut"})
        failure_subtree = self.subtree_action_names(failed_gate)
        self.assertIn("Append_Telemetry_Post_Failed", failure_subtree)
        for name in failure_subtree:
            self.assertNotEqual(self.action(name).get("type"), "Terminate")

    def test_business_events_kql_funnel_queries(self) -> None:
        kql = KQL_PATH.read_text(encoding="utf-8")
        self.assertIn("SOCRadarTaegisBridge_CL", kql)
        self.assertIn("ago(24h)", kql)
        self.assertIn("ago(7d)", kql)
        self.assertIn("bin(TimeGenerated, 1d)", kql)
        self.assertIn("drift_after_close", kql)
        self.assertIn("run_summary", kql)
        self.assertIn("EventType", kql)
        self.assertIn("WindowMode", kql)

    def test_mutation_retry_policies_unchanged_by_telemetry(self) -> None:
        # Exactly the six Taegis mutations keep retryPolicy none; the
        # telemetry POST and all ledger writes stay exponential.
        none_retry = [
            name
            for name, action, _ in self.actions
            if isinstance(action.get("inputs"), dict)
            and action["inputs"].get("retryPolicy") == {"type": "none"}
        ]
        self.assertEqual(len(none_retry), 6, none_retry)
        no_retry = []
        for name, action, _ in self.actions:
            inputs = action.get("inputs")
            if isinstance(inputs, dict) and inputs.get("retryPolicy") == {"type": "none"}:
                no_retry.append(name)
        self.assertEqual(
            sorted(no_retry),
            [
                "Close_Existing_Taegis_Case",
                "Close_New_Taegis_Case",
                "Create_Taegis_Case",
                "Update_Taegis_Case",
                "Update_Taegis_Case_Before_Close",
                "Update_Taegis_Case_Reopen",
            ],
        )

    def test_reopen_policy_is_opt_in_and_gated(self) -> None:
        # (a) Reopen is strictly opt-in: the example deployment keeps manual.
        parameters = json.loads(PARAMETERS_PATH.read_text(encoding="utf-8"))["parameters"]
        self.assertEqual(parameters["reopenPolicy"]["value"], "manual")
        self.assertIn("ReopenPolicy", self.workflow["parameters"])
        bicep = (ROOT / "main.bicep").read_text(encoding="utf-8")
        self.assertIn("param reopenPolicy string = 'manual'", bicep)
        self.assertIn("'auto'", bicep)
        self.assertIn("ReopenPolicy", bicep)
        # (b) The gate demands policy auto, a source that is open again, apply
        # mode, the canonical apply scope, per-alarm isolation, and the cap.
        gate = self.action("If_Auto_Reopen")
        expression = gate["expression"]
        self.assertIn("toLower(parameters('ReopenPolicy')), 'auto'", expression)
        self.assertIn("equals(outputs('Compose_Is_Source_Closed'), false)", expression)
        self.assertIn("toLower(parameters('SyncMode')), 'apply'", expression)
        self.assertIn("parameters('ApplyAllAlarms')", expression)
        self.assertIn("contains(parameters('ApplyAlarmIds'), outputs('Compose_Alarm_ID'))", expression)
        self.assertIn("contains(parameters('ApplyAlarmIds'), int(outputs('Compose_Alarm_ID')))", expression)
        self.assertIn(
            "not(contains(variables('failed_alarm_ids'), outputs('Compose_Alarm_ID')))",
            expression,
        )
        self.assertIn(
            "less(variables('mutation_count'), parameters('MaxMutationsPerRun'))",
            expression,
        )
        # (c) The reopen mutation reserves the cap first, retries never, and
        # writes the fresh re-read ID with the open target status.
        self.assert_is_fresh_case_read(
            "Read_Case_Before_Reopen", "@outputs('Compose_Existing_Case')?['id']"
        )
        reopen = self.action("Update_Taegis_Case_Reopen")
        self.assertEqual(reopen["inputs"]["retryPolicy"], {"type": "none"})
        self.assertEqual(
            reopen["runAfter"], {"Increment_Reopen_Mutation_Count": ["Succeeded"]}
        )
        reopen_input = reopen["inputs"]["body"]["variables"]["input"]
        self.assertEqual(reopen["inputs"]["body"]["operationName"], "updateInvestigationV2")
        self.assertEqual(
            reopen_input["id"],
            "@body('Read_Case_Before_Reopen')?['data']?['investigationV2']?['id']",
        )
        self.assertEqual(reopen_input["status"], "@outputs('Compose_Target_Open_Status')")
        self.assertIn(
            "union(coalesce(body('Read_Case_Before_Reopen')?['data']?['investigationV2']?['tags']",
            reopen_input["tags"],
        )
        # Success evidence: mapping write adopts the new open status, and a
        # permanently failed ledger write marks the alarm failed.
        ledger = self.action("Write_Ledger_Mapping_Reopened")
        self.assertEqual(ledger["inputs"]["method"], "PUT")
        self.assertIn("LedgerMappingTableName", ledger["inputs"]["uri"])
        self.assertEqual(
            ledger["inputs"]["body"]["TargetStatus"],
            "@outputs('Compose_Target_Open_Status')",
        )
        self.assertEqual(
            ledger["inputs"]["body"]["SourceFingerprint"],
            "@outputs('Compose_Source_Fingerprint')",
        )
        handler = self.action("Append_Ledger_Failed_Event_Reopened")
        self.assertEqual(
            handler["runAfter"], {"Write_Ledger_Mapping_Reopened": ["Failed", "TimedOut"]}
        )
        self.assertIn(
            "failed_alarm_ids", json.dumps(self.action("If_Reopen_Has_Errors")["actions"])
        )
        self.assertIn("case_reopened", json.dumps(self.action("Append_Reopen_Result")))
        # Audit mode plans instead of mutating.
        planned = self.action("If_Planned_Reopen")
        self.assertIn("toLower(parameters('SyncMode')), 'audit'", planned["expression"])
        self.assertIn("toLower(parameters('ReopenPolicy')), 'auto'", planned["expression"])
        for child in planned["actions"].values():
            self.assertEqual(child["type"], "AppendToArrayVariable")
        self.assertIn("planned_reopen", json.dumps(planned["actions"]))
        # (d) The pre-existing drift actions survive verbatim on the else path.
        subtree = self.subtree_action_names(gate)
        for name in (
            "If_Closed_Case_Drifted",
            "Append_Drift_After_Close",
            "Write_Ledger_Drift_Event",
            "Append_Ledger_Failed_Event_Drift",
            "Append_Failed_Alarm_Ledger_Drift",
            "Append_Closed_No_Op",
        ):
            with self.subTest(preserved=name):
                self.assertIn(name, subtree)
        # (e) Ownership authority in the reopen chain is the deterministic tag
        # alone: serviceDesk fields never participate.
        reopen_chain_text = json.dumps(gate["actions"])
        self.assertNotIn("serviceDesk", reopen_chain_text)
        conflict = self.action("If_Pre_Reopen_Conflict")
        self.assertIn("Read_Case_Before_Reopen", conflict["expression"])
        self.assertIn("socradar-company-", conflict["expression"])
        self.assertNotIn("serviceDesk", conflict["expression"])
        self.assertIn("failed_alarm_ids", json.dumps(conflict["actions"]))


if __name__ == "__main__":
    unittest.main()
