"""Prompt hygiene: size budget, no direct contradictions, single source of truth, repo AGENTS.md conflicts."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
if str(KIT) not in sys.path:
    sys.path.insert(0, str(KIT))

from lib import prompt_audit  # noqa: E402


class KitPromptHygiene(unittest.TestCase):
    def test_shipped_kit_passes_the_full_audit(self) -> None:
        rep = prompt_audit.audit_kit_prompts(KIT)
        self.assertEqual(rep["findings"], [], rep)
        self.assertTrue(rep["ok"])

    def test_every_role_stays_within_the_declared_prompt_budget(self) -> None:
        roles = json.loads((KIT / "roles.json").read_text(encoding="utf-8"))
        budget = int(roles["policy"]["max_role_prompt_chars"])
        self.assertGreater(budget, 0)
        sizes = prompt_audit.role_auto_load_chars(KIT, roles)
        self.assertEqual(set(sizes), set(roles["roles"]))
        for role, chars in sizes.items():
            self.assertLessEqual(chars, budget, f"{role} auto-loads {chars} chars (budget {budget})")

    def test_conflicting_ub_mode_is_not_loaded_into_any_role(self) -> None:
        roles = json.loads((KIT / "roles.json").read_text(encoding="utf-8"))
        self.assertEqual(roles["roles"]["developer"].get("codex_skills"), [])
        for role, spec in roles["roles"].items():
            self.assertNotIn("ub-mode", spec.get("team", []), role)
            self.assertNotIn("ub-mode", spec.get("auto_load", []), role)
        self.assertIn("ub-mode", roles["obsolete_skills"]["codex"])
        codex = (KIT / "codex" / "AGENTS.md").read_text(encoding="utf-8")
        for phrase, _ in prompt_audit.FORBIDDEN_DEVELOPER_PHRASES:
            self.assertNotIn(phrase, codex)

    def test_on_demand_skills_exist_and_are_not_auto_loaded_by_workers(self) -> None:
        roles = json.loads((KIT / "roles.json").read_text(encoding="utf-8"))
        for sk in ("devflow-chat", "devflow-cli"):
            self.assertTrue((KIT / "skills" / sk / "SKILL.md").is_file(), sk)
        for role, spec in roles["roles"].items():
            self.assertFalse({"devflow-chat", "devflow-cli"} & set(spec["auto_load"]), role)
            self.assertIn("devflow-chat", spec["team"], role)
            self.assertIn("devflow-cli", spec["team"], role)

    def test_dev_and_ops_card_creation_guidance_always_carries_the_structural_fields(self) -> None:
        for name in ("devflow-orchestrate", "devflow-devops"):
            text = (KIT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("Allowed-Scope:", text, name)
            self.assertIn("goal_mode=true", text, name)
            self.assertIn("## 本卡完成", text, name)
        orch = (KIT / "skills" / "devflow-orchestrate" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Out-of-Scope:", orch)
        self.assertIn("Non-Goals:", orch)
        for mode in ("deliver", "investigate", "review"):
            self.assertIn(mode, orch)

    def test_developer_is_pointed_at_the_single_shift_left_submit_entry(self) -> None:
        for p in (KIT / "skills" / "devflow-develop" / "SKILL.md", KIT / "codex" / "AGENTS.md"):
            self.assertIn("devflow pr submit", p.read_text(encoding="utf-8"), str(p))

    def test_audit_detects_injected_contradictions_and_budget_overrun(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="devflow-audit-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        kit = tmp / "kit"
        shutil.copytree(KIT, kit, ignore=shutil.ignore_patterns("__pycache__", "tests", "templates", "vendor"))
        with (kit / "codex" / "AGENTS.md").open("a", encoding="utf-8") as fh:
            fh.write("\n* Push to the main line at the end.\n")
        with (kit / "skills" / "devflow-orchestrate" / "SKILL.md").open("a", encoding="utf-8") as fh:
            fh.write("\n禁止把檔案路徑寫進卡片。\n")
        roles = json.loads((kit / "roles.json").read_text(encoding="utf-8"))
        roles["policy"]["max_role_prompt_chars"] = 100
        (kit / "roles.json").write_text(json.dumps(roles), encoding="utf-8")
        rep = prompt_audit.audit_kit_prompts(kit)
        self.assertFalse(rep["ok"])
        joined = "\n".join(rep["findings"])
        self.assertIn("Push to the main line", joined)
        self.assertIn("禁止寫檔案路徑", joined)
        self.assertIn("超過上限 100", joined)


class RepoAgentsConflictScan(unittest.TestCase):
    def _scan(self, text: str | None) -> list[dict[str, str]]:
        d = Path(tempfile.mkdtemp(prefix="devflow-agents-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        if text is not None:
            (d / "AGENTS.md").write_text(text, encoding="utf-8")
        return prompt_audit.scan_repo_agents_conflicts(d)

    def test_clean_or_missing_agents_md_has_no_warnings(self) -> None:
        self.assertEqual(self._scan("# Guide\n\nPython 四空格；測試用 unittest。\n"), [])
        self.assertEqual(self._scan("未獲本次寫入授權不得自行開遠端 issue／Project 或更改設定。\n"), [])
        self.assertEqual(self._scan(None), [])
        self.assertEqual(prompt_audit.scan_repo_agents_conflicts(None), [])

    def test_detects_workflow_directives_that_clash_with_devflow(self) -> None:
        ids = {w["id"] for w in self._scan("devops 為唯一 source/publisher；PR 一律準備 **Draft PR**。\n")}
        self.assertEqual(ids, {"sole_publisher_conflict", "draft_pr_only"})
        ids = {w["id"] for w in self._scan("當前工作僅本地修改，並未取得建立 issue／PR 或其他 GitHub 寫入的新授權。\n")}
        self.assertIn("forbids_pr_creation", ids)


if __name__ == "__main__":
    unittest.main()
