"""Unit tests: no network, all HTTP boundaries mocked."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

from django.test import TestCase

from .conf import MAX_FINDINGS, MAX_INLINE_COMMENTS, Conf, RepoToken, get_conf, reload_conf
from .models import RepoTokenEntry, ReviewRecord, Setting, WebhookEvent, try_register_review
from .services.diffctx import (
    build_diff_context,
    build_inventory_section,
    estimate_tokens,
    number_patch_lines,
)
from .services.gitlab import GitLabError
from .services.llm import (
    LLMConfig,
    LLMError,
    ReviewOutput,
    chat,
    extract_yaml_block,
    filter_unverified_absence_issues,
    parse_review,
    review_with_fallback,
)
from .services.prompts import build_system_prompt, build_user_prompt
from .services.repoctx import build_repo_context, extract_symbols
from .services.render import (
    PROGRESS_MARKER,
    REVIEW_MARKER,
    render_failure,
    render_progress,
    render_review,
    render_skip,
    truncate,
)
from .services.mrref import MRRefError, parse_mr_ref
from .services.tokens import resolve_repo_token


def _llm_config(conf: Conf, fallback: tuple[str, ...] = ()) -> LLMConfig:
    return LLMConfig(
        base_url=conf.llm_base_url,
        api_key=conf.llm_api_key,
        model=conf.model,
        fallback_models=fallback,
    )


def _conf(**overrides) -> Conf:
    base = dict(
        webhook_secret="whsec",
        default_api_url="https://gitlab.example.com",
        default_token="default-token",
        repo_tokens=(
            RepoToken(prefix="backend", api_url="https://gitlab.example.com", token="tok-backend"),
            RepoToken(prefix="backend/team-a", api_url="https://gitlab.example.com", token="tok-team-a"),
        ),
        llm_base_url="https://llm.example.com/v1",
        llm_api_key="llm-key",
        model="test-model",
        inline_comments=True,
    )
    base.update(overrides)
    return Conf(**base)


SAMPLE_PATCH = """@@ -1,3 +1,4 @@
 context line 1
-removed line
+added line
 context line 2
@@ -10,2 +11,3 @@
 ctx
+second added
"""

DEF_PATCH = """@@ -1,2 +1,4 @@
 import os
+def process_payment(amount):
+    return amount
"""

# Dense patch: every line of the new file 1..20 is a hunk line, so tests can
# place inline comments on any of those lines.
DENSE_PATCH = "@@ -0,0 +1,20 @@\n" + "".join(f"+line {n}\n" for n in range(1, 21))

GOOD_REVIEW_YAML = (
    "review:\n"
    "  overall_assessment: ok\n"
    "  score: 88\n"
    "  risk_level: low\n"
    "  key_issues_to_review:\n"
    "    - relevant_file: app.py\n"
    "      issue_header: Bug\n"
    "      issue_content: bad\n"
    "      start_line: 2\n"
    "      end_line: 2\n"
    "  relevant_tests: No\n"
    "  security_concerns: No\n"
)


class TokenResolverTests(TestCase):
    def test_longest_prefix_wins(self):
        conf = _conf()
        self.assertEqual(resolve_repo_token(conf, "backend/team-a/svc").token, "tok-team-a")
        self.assertEqual(resolve_repo_token(conf, "backend/payments").token, "tok-backend")

    def test_prefix_is_a_plain_case_insensitive_string_prefix(self):
        # Not segment aware: `tp/cneos` must cover `tp/CNEOSv2/...`, and casing
        # on either side must not matter.
        conf = _conf(
            default_token="",
            repo_tokens=(
                RepoToken(prefix="tp/cneos", api_url="https://x", token="tok-cneos"),
            ),
        )
        for path in (
            "tp/CNEOSv2/NetF/npp/npp.v2.0",
            "TP/cneosv2/netf/npp/npp.v2.0",
            "tp/cneos",
            "tp/cneos/",
        ):
            self.assertEqual(resolve_repo_token(conf, path).token, "tok-cneos", path)

    def test_prefix_forms_are_equivalent(self):
        # tp, tp/ and TP/ all normalise to the same prefix.
        for typed in ("tp", "tp/", "TP", "TP/", " tp/ "):
            conf = _conf(default_token="", repo_tokens=(RepoToken(prefix=typed, api_url="https://x", token="tok-tp"),))
            self.assertEqual(resolve_repo_token(conf, "tp/CNEOSv2/NetF/npp").token, "tok-tp", typed)

    def test_longest_match_wins_across_prefix_set(self):
        conf = _conf(
            default_token="",
            repo_tokens=(
                RepoToken(prefix="tp", api_url="https://x", token="tok-tp"),
                RepoToken(prefix="tp/CNE", api_url="https://x", token="tok-cne"),
                RepoToken(prefix="tp/cneosv2/netf/npp/", api_url="https://x", token="tok-npp"),
            ),
        )
        self.assertEqual(resolve_repo_token(conf, "tp/CNEOSv2/NetF/npp/npp.v2.0").token, "tok-npp")
        self.assertEqual(resolve_repo_token(conf, "tp/CNEOSv2/OS/kernel").token, "tok-cne")
        self.assertEqual(resolve_repo_token(conf, "tp/other/repo").token, "tok-tp")

    def test_default_fallback_and_none(self):
        self.assertEqual(resolve_repo_token(_conf(), "anything/else").token, "default-token")
        self.assertIsNone(resolve_repo_token(_conf(default_token=""), "anything/else"))


class DiffContextTests(TestCase):
    def test_number_patch_lines(self):
        rendered = number_patch_lines(SAMPLE_PATCH)
        self.assertIn("1   context line 1", rendered.text)
        self.assertIn("- removed line", rendered.text)
        self.assertIn("2 + added line", rendered.text)  # removed line does not consume new numbering
        self.assertIn("12 + second added", rendered.text)
        self.assertEqual(rendered.max_new_line, 12)
        # only lines that exist in the new file are commentable; the gap 4..10 is not
        self.assertEqual(rendered.new_lines, frozenset({1, 2, 3, 11, 12}))

    def test_commentable_lines_only_cover_reviewed_files(self):
        changes = [
            {"new_path": "app.py", "old_path": "app.py", "diff": SAMPLE_PATCH},
            {"new_path": "package-lock.json", "old_path": "package-lock.json", "diff": SAMPLE_PATCH},
        ]
        ctx = build_diff_context(changes, max_tokens=10000)
        commentable = ctx.commentable_lines_by_file
        self.assertEqual(commentable, {"app.py": frozenset({1, 2, 3, 11, 12})})
        self.assertNotIn("package-lock.json", commentable)  # filtered file: no inline comments

    def test_filter_and_inventory(self):
        changes = [
            {"new_path": "app.py", "old_path": "app.py", "diff": SAMPLE_PATCH},
            {"new_path": "package-lock.json", "old_path": "package-lock.json", "diff": SAMPLE_PATCH},
            {"new_path": "new.py", "old_path": "new.py", "diff": SAMPLE_PATCH, "new_file": True},
        ]
        ctx = build_diff_context(changes, max_tokens=10000)
        self.assertEqual(ctx.filtered_files, ["package-lock.json"])
        self.assertEqual(ctx.added_files, ["new.py"])
        self.assertEqual(sorted(ctx.reviewed_files), ["app.py", "new.py"])

    def test_clipping_drops_files_when_over_budget(self):
        big_patch = SAMPLE_PATCH * 200
        changes = [{"new_path": f"f{i}.py", "old_path": f"f{i}.py", "diff": big_patch} for i in range(4)]
        ctx = build_diff_context(changes, max_tokens=estimate_tokens(big_patch) + 50)
        self.assertLess(len(ctx.reviewed_files), 4)
        self.assertTrue(ctx.not_reviewed_files)
        self.assertIn("FILES NOT INCLUDED", build_inventory_section(ctx))

    def test_binary_or_empty_patch_filtered(self):
        ctx = build_diff_context([{"new_path": "img.png", "old_path": "img.png", "diff": ""}], max_tokens=100)
        self.assertEqual(ctx.filtered_files, ["img.png"])


class RepoCtxTests(TestCase):
    def _ctx(self, changes: list[dict]):
        return build_diff_context(changes, max_tokens=10000)

    def test_extract_symbols_from_definitions_and_new_files(self):
        ctx = self._ctx(
            [
                {"new_path": "app.py", "old_path": "app.py", "diff": DEF_PATCH},
                {
                    "new_path": "pkg/brand_new_module.py",
                    "old_path": "pkg/brand_new_module.py",
                    "diff": "@@ -0,0 +1,1 @@\n+value = 1\n",
                    "new_file": True,
                },
            ]
        )
        self.assertEqual(extract_symbols(ctx, limit=8), ["process_payment", "brand_new_module"])

    def test_extract_symbols_skips_unreviewed_and_caps(self):
        big = DEF_PATCH * 3
        ctx = self._ctx(
            [
                {"new_path": "app.py", "old_path": "app.py", "diff": big},
                {"new_path": "skip.md", "old_path": "skip.md", "diff": DEF_PATCH},  # ignored glob
            ]
        )
        self.assertNotIn("skip.md", ctx.reviewed_files)
        symbols = extract_symbols(ctx, limit=2)
        self.assertEqual(symbols, ["process_payment"])  # deduped, filtered file not scanned

    def test_build_repo_context_fetches_files_and_references(self):
        ctx = self._ctx([{"new_path": "app.py", "old_path": "app.py", "diff": DEF_PATCH}])
        client = MagicMock()
        client.get_file_content.return_value = "import os\n\ndef run():\n    pass\n"
        client.search_blobs.return_value = [
            {"path": "src/caller.py", "data": "process_payment(1)", "startline": 42},
            {"path": "app.py", "data": "def process_payment(amount)", "startline": 2},  # in-diff: excluded
        ]
        text, stats = build_repo_context(client, "g/r", "a" * 40, ctx)
        self.assertIn("Full content: `app.py`", text)
        self.assertIn("    1  import os", text)  # numbered lines make path:LINE citable
        client.search_blobs.assert_called_once_with("g/r", "process_payment", "a" * 40)
        self.assertIn("src/caller.py:42", text)
        self.assertNotIn("app.py:2", text)
        self.assertEqual(stats["files"], ["app.py"])
        self.assertEqual(stats["symbols"], ["process_payment"])
        self.assertTrue(stats["search_available"])

    def test_search_unavailable_keeps_file_context(self):
        ctx = self._ctx([{"new_path": "app.py", "old_path": "app.py", "diff": DEF_PATCH}])
        client = MagicMock()
        client.get_file_content.return_value = "content"
        client.search_blobs.return_value = None  # advanced search not enabled
        text, stats = build_repo_context(client, "g/r", "a" * 40, ctx)
        self.assertIn("Full content: `app.py`", text)
        self.assertNotIn("References found", text)
        self.assertFalse(stats["search_available"])
        self.assertEqual(stats["symbols"], [])

    def test_api_failures_degrade_to_empty_context(self):
        ctx = self._ctx([{"new_path": "app.py", "old_path": "app.py", "diff": DEF_PATCH}])
        client = MagicMock()
        client.get_file_content.side_effect = GitLabError("GET raw returned 404", status=404)
        client.search_blobs.side_effect = GitLabError("GET search returned 500", status=500)
        text, stats = build_repo_context(client, "g/r", "a" * 40, ctx)
        self.assertEqual(text, "")
        self.assertEqual(stats["files"], [])

    def test_non_string_responses_degrade_silently(self):
        """Plain MagicMock client (pipeline tests): nothing usable -> no context."""
        ctx = self._ctx([{"new_path": "app.py", "old_path": "app.py", "diff": DEF_PATCH}])
        text, stats = build_repo_context(MagicMock(), "g/r", "a" * 40, ctx)
        self.assertEqual(text, "")
        self.assertEqual(stats["files"], [])
        self.assertFalse(stats["search_available"])


class AbsenceFilterTests(TestCase):
    def _review_with(self, issue_yaml: str) -> ReviewOutput:
        return parse_review(
            "review:\n  overall_assessment: ok\n  score: 70\n"
            f"  key_issues_to_review:\n{issue_yaml}"
            "  relevant_tests: No\n  security_concerns: No\n",
            num_max_findings=6,
        )

    def test_unverified_unused_claim_dropped(self):
        review = self._review_with(
            "    - relevant_file: app.py\n      issue_header: Unused\n"
            "      issue_content: helper_process is unused and can be removed\n"
            "      start_line: 2\n      end_line: 2\n"
        )
        filtered, dropped = filter_unverified_absence_issues(review)
        self.assertEqual(len(dropped), 1)
        self.assertEqual(filtered.key_issues_to_review, [])

    def test_claim_with_real_evidence_kept(self):
        review = self._review_with(
            "    - relevant_file: app.py\n      issue_header: Unused\n"
            "      issue_content: helper_process is unused - repo search found no reference\n"
            "      start_line: 2\n      end_line: 2\n"
            "      kind: verified\n      evidence: 'blob search: 0 hits'\n"
        )
        filtered, dropped = filter_unverified_absence_issues(review)
        self.assertEqual(dropped, [])
        self.assertEqual(len(filtered.key_issues_to_review), 1)
        self.assertEqual(filtered.key_issues_to_review[0].evidence, "blob search: 0 hits")

    def test_patch_only_evidence_does_not_save_absence_claim(self):
        review = self._review_with(
            "    - relevant_file: app.py\n      issue_header: Missing\n"
            "      issue_content: this symbol is not defined anywhere in the repo\n"
            "      start_line: 2\n      end_line: 2\n"
            "      kind: verified\n      evidence: patch-only\n"
        )
        filtered, dropped = filter_unverified_absence_issues(review)
        self.assertEqual(len(dropped), 1)

    def test_diff_content_suggestions_are_never_filtered(self):
        review = self._review_with(
            "    - relevant_file: app.py\n      issue_header: Robustness\n"
            "      issue_content: 新增代码缺少对空输入的错误处理，会在 amount 为 None 时抛异常\n"
            "      start_line: 2\n      end_line: 2\n      kind: suggestion\n      evidence: patch-only\n"
            "    - relevant_file: app.py\n      issue_header: Bug\n"
            "      issue_content: off-by-one in the new loop bounds\n"
            "      start_line: 3\n      end_line: 3\n"
        )
        filtered, dropped = filter_unverified_absence_issues(review)
        self.assertEqual(dropped, [])
        self.assertEqual(len(filtered.key_issues_to_review), 2)


class PromptTests(TestCase):
    def test_findings_ordered_by_complexity_then_length(self):
        system = build_system_prompt(6, "zh-CN")
        self.assertIn("most complex issue first", system)
        self.assertIn("equally complex", system)
        self.assertIn("inline comments in this exact order", system)
        self.assertIn("Never pad issue_content", system)

    def test_prompt_restricts_comments_to_lines_in_the_diff(self):
        system = build_system_prompt(6, "zh-CN")
        self.assertIn("actually present in that file's diff", system)
        self.assertIn("Never point at a file the diff", system)

    def test_prompt_declares_context_boundary_and_evidence_rule(self):
        system = build_system_prompt(6, "zh-CN")
        self.assertIn("Context boundary", system)
        self.assertIn("is allowed ONLY", system)
        self.assertIn("assume you cannot see", system)
        self.assertIn("kind: verified", system)
        self.assertIn("kind: suggestion", system)

    def test_user_prompt_embeds_repo_context_as_evidence(self):
        ctx = build_diff_context([{"new_path": "app.py", "old_path": "app.py", "diff": SAMPLE_PATCH}], max_tokens=10000)
        kwargs = dict(
            title="t", description="", author="a", source_branch="f", target_branch="m", commit_messages=""
        )
        user = build_user_prompt(**kwargs, diff_ctx=ctx, repo_context="REPOCTX-MARKER")
        self.assertIn("cite it as evidence", user)
        self.assertIn("REPOCTX-MARKER", user)
        self.assertNotIn("Repository context", build_user_prompt(**kwargs, diff_ctx=ctx))


class LLMParseTests(TestCase):
    def test_extract_yaml_block(self):
        text = "Some prose\n```yaml\nreview:\n  score: 80\n```\n"
        self.assertEqual(extract_yaml_block(text), "review:\n  score: 80")

    def test_parse_nested_review(self):
        review = parse_review(f"```yaml\n{GOOD_REVIEW_YAML}```", num_max_findings=6)
        self.assertEqual(review.score, 88)
        self.assertEqual(review.risk_level, "low")
        self.assertEqual(review.key_issues_to_review[0].relevant_file, "app.py")

    def test_parse_kind_and_evidence_fields(self):
        review = parse_review(
            "review:\n  key_issues_to_review:\n"
            "    - relevant_file: app.py\n      issue_content: x\n"
            "      start_line: 1\n      end_line: 1\n"
            "      kind: Verified\n      evidence: 'src/a.py:12'\n",
            num_max_findings=6,
        )
        issue = review.key_issues_to_review[0]
        self.assertEqual(issue.kind, "verified")  # normalized to lowercase literal
        self.assertEqual(issue.evidence, "src/a.py:12")

    def test_kind_and_evidence_default_empty(self):
        review = parse_review(GOOD_REVIEW_YAML, num_max_findings=6)
        issue = review.key_issues_to_review[0]
        self.assertEqual(issue.kind, "")
        self.assertEqual(issue.evidence, "")

    def test_invalid_kind_and_non_string_evidence_normalised(self):
        review = parse_review(
            "review:\n  key_issues_to_review:\n"
            "    - relevant_file: app.py\n      issue_content: x\n"
            "      start_line: 1\n      end_line: 1\n"
            "      kind: maybe\n      evidence: 42\n",
            num_max_findings=6,
        )
        issue = review.key_issues_to_review[0]
        self.assertEqual(issue.kind, "")
        self.assertEqual(issue.evidence, "")

    def test_repair_broken_colon_value(self):
        raw = "review:\n  overall_assessment: uses config: default path\n  score: 70\n"
        review = parse_review(raw, num_max_findings=6)
        self.assertEqual(review.score, 70)
        self.assertIn("config: default", review.overall_assessment)

    def test_findings_capped(self):
        issues = "\n".join(
            f"    - relevant_file: f{i}.py\n      issue_content: x\n      start_line: 1\n      end_line: 1"
            for i in range(5)
        )
        review = parse_review(f"review:\n  key_issues_to_review:\n{issues}", num_max_findings=2)
        self.assertEqual(len(review.key_issues_to_review), 2)

    def test_invalid_schema_raises(self):
        with self.assertRaises(LLMError) as ctx:
            parse_review("review:\n  score: not-a-number\n", num_max_findings=6)
        self.assertEqual(ctx.exception.reason, "bad_schema")

    def test_non_mapping_raises(self):
        with self.assertRaises(LLMError) as ctx:
            parse_review("just some text", num_max_findings=6)
        self.assertEqual(ctx.exception.reason, "bad_yaml")


class LLMChatTests(TestCase):
    def _response(self, content: str, finish_reason: str = "stop", status: int = 200):
        resp = MagicMock(status_code=status)
        resp.json.return_value = {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}
        return resp

    @patch("reviewer.services.llm.requests.post")
    def test_chat_ok(self, post):
        post.return_value = self._response("hello")
        text, reason = chat(_llm_config(_conf()), "sys", "usr")
        self.assertEqual(text, "hello")
        self.assertEqual(reason, "stop")

    @patch("reviewer.services.llm.requests.post")
    def test_chat_truncated_raises(self, post):
        post.return_value = self._response("partial", finish_reason="length")
        with self.assertRaises(LLMError) as ctx:
            chat(_llm_config(_conf()), "sys", "usr")
        self.assertEqual(ctx.exception.reason, "truncated")

    @patch("reviewer.services.llm.requests.post")
    def test_fallback_to_second_model(self, post):
        post.side_effect = [MagicMock(status_code=500), self._response("review:\n  score: 50\n")]
        review, model = review_with_fallback(_llm_config(_conf(), fallback=("backup-model",)), "sys", "usr")
        self.assertEqual(model, "backup-model")
        self.assertEqual(review.score, 50)


class RenderTests(TestCase):
    def _review(self) -> ReviewOutput:
        return parse_review(
            "review:\n  overall_assessment: ok\n  score: 90\n  risk_level: low\n"
            "  key_issues_to_review: []\n  relevant_tests: yes\n  security_concerns: No\n",
            num_max_findings=6,
        )

    def test_review_body_contains_marker_and_sections(self):
        body = render_review(self._review(), mr_url="http://x/mr/1", remaining_files=["b.py"], model_used="m1")
        self.assertIn(REVIEW_MARKER, body)
        self.assertIn("b.py", body)
        self.assertIn("m1", body)

    def test_failure_sanitized(self):
        body = render_failure("http_error")
        self.assertIn(REVIEW_MARKER, body)
        self.assertIn("http_error", body)
        self.assertNotIn("Traceback", body)

    def test_truncate_keeps_marker(self):
        body = "x" * 5000 + REVIEW_MARKER
        cut = truncate(body, 1000)
        self.assertLessEqual(len(cut), 1000)
        self.assertTrue(cut.endswith(REVIEW_MARKER))

    def test_progress_marker(self):
        self.assertIn(PROGRESS_MARKER, render_progress())

    def test_skip_body_explains_reason(self):
        body = render_skip("no reviewable files")
        self.assertIn("未进行 AI 评审", body)
        self.assertIn("没有评审价值", body)
        self.assertIn("没有可评审的代码文件", body)


class RegisterTests(TestCase):
    def test_register_flow(self):
        args = dict(project_path="g/r", mr_iid=1, head_sha="a" * 40)
        rec = try_register_review(**args)
        self.assertEqual(rec.status, "running")
        self.assertIsNone(try_register_review(**args))  # in-flight
        rec.status = "success"
        rec.save()
        self.assertIsNone(try_register_review(**args))  # done
        rec.status = "failed"
        rec.save()
        retried = try_register_review(**args)
        self.assertIsNotNone(retried)  # failed allows retry
        self.assertEqual(retried.status, "running")
        self.assertEqual(ReviewRecord.objects.count(), 1)

    def test_force_reruns_success(self):
        args = dict(project_path="g/r", mr_iid=1, head_sha="a" * 40)
        rec = try_register_review(**args)
        rec.status = "success"
        rec.save()
        forced = try_register_review(**args, force=True)
        self.assertIsNotNone(forced)
        self.assertEqual(forced.status, "running")
        self.assertEqual(ReviewRecord.objects.count(), 1)


class WebhookViewTests(TestCase):
    def setUp(self):
        patcher = patch("reviewer.views.get_conf", return_value=_conf())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _mr_payload(self, project="backend/payments", iid=7, action="open", title="Add feature", user="alice"):
        return {
            "object_kind": "merge_request",
            "project": {"path_with_namespace": project},
            "user": {"username": user},
            "object_attributes": {
                "iid": iid,
                "action": action,
                "title": title,
                "target_branch": "main",
                "source_branch": "feat",
                "last_commit": {"id": "c" * 40},
            },
        }

    def _post(self, payload: dict, token: str = "whsec"):
        return self.client.post(
            "/webhook/gitlab/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_GITLAB_TOKEN=token,
        )

    def test_bad_token_rejected(self):
        resp = self._post({"object_kind": "push"}, token="wrong")
        self.assertEqual(resp.status_code, 403)

    def test_no_secret_configured_accepts_unverified(self):
        conf = _conf(webhook_secret="", repo_tokens=())
        with patch("reviewer.views.get_conf", return_value=conf), patch("reviewer.views.submit_review") as submit:
            resp = self._post(self._mr_payload(project="any/repo"))
        self.assertEqual(resp.status_code, 200)
        submit.assert_called_once_with("any/repo", 7, source="webhook")

    def test_open_triggers_review(self):
        with patch("reviewer.views.submit_review") as submit:
            resp = self._post(self._mr_payload())
        self.assertEqual(resp.status_code, 200)
        submit.assert_called_once_with("backend/payments", 7, source="webhook")
        self.assertEqual(WebhookEvent.objects.get().handled, "review")

    def test_push_triggers_when_enabled(self):
        payload = self._mr_payload(action="update")
        payload["object_attributes"]["oldrev"] = "0" * 40
        with patch("reviewer.views.submit_review") as submit, patch("reviewer.views.get_conf",
                                                                    return_value=_conf(review_on_push=True)):
            self._post(payload)
        submit.assert_called_once()

    def test_bot_and_draft_skipped(self):
        with patch("reviewer.views.submit_review") as submit:
            self._post(self._mr_payload(user="some-bot"))
            self._post(self._mr_payload(title="Draft: T"))
        submit.assert_not_called()

    def test_unknown_repo_prefix_ignored_when_no_default(self):
        with patch("reviewer.views.get_conf", return_value=_conf(default_token="")), \
                patch("reviewer.views.submit_review") as submit:
            self._post(self._mr_payload(project="unknown/repo"))
        submit.assert_not_called()
        # first-open audit policy: the open is stored once (with its ignore reason), nothing else
        event = WebhookEvent.objects.get()
        self.assertEqual((event.action, event.handled), ("open", "ignored"))

    def test_only_first_open_event_recorded(self):
        with patch("reviewer.views.submit_review"):
            self._post(self._mr_payload())
            self._post(self._mr_payload(action="reopen"))
            self._post(self._mr_payload())
        self.assertEqual(WebhookEvent.objects.count(), 1)
        self.assertEqual(WebhookEvent.objects.get().action, "open")

    def test_note_review_command(self):
        payload = {
            "object_kind": "note",
            "project": {"path_with_namespace": "backend/payments"},
            "user": {"username": "alice"},
            "object_attributes": {"note": "/review please"},
            "merge_request": {"iid": 3},
        }
        with patch("reviewer.views.submit_review") as submit:
            self._post(payload)
        submit.assert_called_once_with("backend/payments", 3, source="note")

    def test_per_repo_secret_accepted(self):
        conf = _conf(repo_tokens=(
            RepoToken(prefix="backend", api_url="https://x", token="t", webhook_secret="repo-secret"),
        ))
        with patch("reviewer.views.get_conf", return_value=conf), patch("reviewer.views.submit_review"):
            resp = self.client.post(
                "/webhook/gitlab/",
                data=json.dumps(self._mr_payload()),
                content_type="application/json",
                HTTP_X_GITLAB_TOKEN="repo-secret",
            )
        self.assertEqual(resp.status_code, 200)


class PipelineTests(TestCase):
    def setUp(self):
        self.conf = _conf()
        patcher = patch("reviewer.services.pipeline.get_conf", return_value=self.conf)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _client_mock(self):
        client = MagicMock()
        client.get_merge_request.return_value = {
            "sha": "a" * 40,
            "web_url": "https://gl/g/r/-/merge_requests/1",
            "title": "Add feature",
            "description": "does things",
            "author": {"name": "alice"},
            "source_branch": "feat",
            "target_branch": "main",
            "merge_commit_message": "",
            "draft": False,
            "work_in_progress": False,
        }
        client.get_mr_changes.return_value = {
            "diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": "h"},
            "changes": [{"new_path": "app.py", "old_path": "app.py", "diff": SAMPLE_PATCH}],
        }
        client.get_own_user_id.return_value = 7
        client.list_notes.return_value = []
        client.list_discussions.return_value = []
        note_ids = iter([{"id": 55}, {"id": 66}, {"id": 77}])
        client.create_note.side_effect = lambda *a, **k: next(note_ids)
        return client

    @patch("reviewer.services.pipeline.review_with_fallback")
    @patch("reviewer.services.pipeline.GitLabClient")
    def test_success_flow(self, client_cls, review_fn):
        client = self._client_mock()
        client_cls.return_value = client
        # summary-only mode: issues stay in the comment body
        with patch("reviewer.services.pipeline.get_conf", return_value=_conf(inline_comments=False)):
            review_fn.return_value = (parse_review(GOOD_REVIEW_YAML, num_max_findings=6), "test-model")
            from .services.pipeline import run_review

            run_review("backend/payments", 1, source="webhook")

        record = ReviewRecord.objects.get()
        self.assertEqual(record.status, "success")
        self.assertEqual(record.model_used, "test-model")
        self.assertEqual(record.issues_count, 1)
        self.assertEqual(record.score, 88)
        client.create_note.assert_any_call("backend/payments", 1, render_progress())
        client.delete_note.assert_called_once_with("backend/payments", 1, 55)
        bodies = [call.args[-1] for call in client.create_note.call_args_list]
        self.assertTrue(any(REVIEW_MARKER in b and "Bug" in b for b in bodies))
        statuses = {s["step"]: s["status"] for s in record.steps}
        self.assertEqual(
            statuses,
            {"token": "success", "read_mr": "success", "fetch_diff": "success",
             "ai_review": "success", "publish": "success"},
        )
        self.assertTrue(all(s["detail"] for s in record.steps))  # every stage carries a log line

    @patch("reviewer.services.pipeline.review_with_fallback")
    @patch("reviewer.services.pipeline.GitLabClient")
    def test_llm_failure_publishes_failure_comment(self, client_cls, review_fn):
        client = self._client_mock()
        client_cls.return_value = client
        review_fn.side_effect = LLMError("http_error", "status 500")
        from .services.pipeline import run_review

        run_review("backend/payments", 1)
        record = ReviewRecord.objects.get()
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.error_reason, "http_error")
        bodies = [call.args[-1] for call in client.create_note.call_args_list]
        self.assertTrue(any("评审失败" in b for b in bodies))
        client.delete_note.assert_called_once()  # progress note removed on failure too

    @patch("reviewer.services.pipeline.review_with_fallback")
    @patch("reviewer.services.pipeline.GitLabClient")
    def test_context_enrichment_failure_does_not_break_review(self, client_cls, review_fn):
        client = self._client_mock()
        client.get_file_content.side_effect = GitLabError("GET raw returned 500", status=500)
        client_cls.return_value = client
        review_fn.return_value = (parse_review(GOOD_REVIEW_YAML, num_max_findings=6), "test-model")
        from .services.pipeline import run_review

        run_review("backend/payments", 1)
        record = ReviewRecord.objects.get()
        self.assertEqual(record.status, "success")
        self.assertEqual(record.issues_count, 1)

    @patch("reviewer.services.pipeline.GitLabClient")
    def test_read_failure_recorded_for_dashboard(self, client_cls):
        client = MagicMock()
        client_cls.return_value = client
        client.get_merge_request.side_effect = GitLabError("gitlab GET /projects/x returned 401", status=401)
        from .services.pipeline import run_review

        run_review("backend/payments", 1, source="webhook")
        record = ReviewRecord.objects.get()
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.error_reason, "read_mr_failed")
        self.assertIn("401", record.error_detail)
        client.create_note.assert_not_called()  # nothing published when MR is unreadable

    @patch("reviewer.services.pipeline.GitLabClient")
    def test_no_token_failure_visible(self, client_cls):
        with patch("reviewer.services.pipeline.get_conf", return_value=_conf(default_token="")):
            from .services.pipeline import run_review

            run_review("unknown/project", 1)
        record = ReviewRecord.objects.get()
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.error_reason, "no_token")
        statuses = {s["step"]: s["status"] for s in record.steps}
        self.assertEqual(statuses["token"], "failed")
        client_cls.assert_not_called()

    @patch("reviewer.services.pipeline.GitLabClient")
    def test_draft_skip_publishes_comment_and_steps(self, client_cls):
        client = self._client_mock()
        client.get_merge_request.return_value["draft"] = True
        client_cls.return_value = client
        from .services.pipeline import run_review

        run_review("backend/payments", 1)
        record = ReviewRecord.objects.get()
        self.assertEqual(record.status, "skipped")
        self.assertEqual(record.error_detail, "draft MR")
        bodies = [call.args[-1] for call in client.create_note.call_args_list]
        self.assertTrue(any("未进行 AI 评审" in b for b in bodies))  # skipped MRs are never silent
        statuses = {s["step"]: s["status"] for s in record.steps}
        self.assertEqual(statuses["ai_review"], "skipped")
        self.assertEqual(statuses["publish"], "success")

    @patch("reviewer.services.pipeline.GitLabClient")
    def test_duplicate_sha_not_reprocessed(self, client_cls):
        client = self._client_mock()
        client_cls.return_value = client
        from .services.pipeline import run_review

        try_register_review(project_path="backend/payments", mr_iid=1, head_sha="a" * 40)
        rec = ReviewRecord.objects.get()
        rec.status = "success"
        rec.save()
        run_review("backend/payments", 1)
        client.create_note.assert_not_called()

    @patch("reviewer.services.pipeline.review_with_fallback")
    @patch("reviewer.services.pipeline.GitLabClient")
    def test_inline_comments_published_with_position(self, client_cls, review_fn):
        client = self._client_mock()
        client_cls.return_value = client
        review_fn.return_value = (parse_review(GOOD_REVIEW_YAML, num_max_findings=6), "test-model")
        from .services.pipeline import run_review

        run_review("backend/payments", 1)
        client.create_inline_discussion.assert_called_once()
        position = client.create_inline_discussion.call_args.args[3]
        self.assertEqual(position["new_line"], 1)  # 0-based
        self.assertEqual(position["base_sha"], "b")
        self.assertEqual(position["head_sha"], "h")
        # issue published inline is removed from the summary comment
        bodies = [call.args[-1] for call in client.create_note.call_args_list]
        summary = [b for b in bodies if REVIEW_MARKER in b and "AI Review" in b][0]
        self.assertNotIn("**1. Bug**", summary)

    @staticmethod
    def _many_issue_review(count: int) -> ReviewOutput:
        """A review with *count* issues on app.py (the patch exposes lines 1..12)."""
        issues = "".join(
            f"    - relevant_file: app.py\n"
            f"      issue_header: Bug {n}\n"
            f"      issue_content: bad {n}\n"
            f"      start_line: {n + 1}\n"
            f"      end_line: {n + 1}\n"
            for n in range(count)
        )
        return parse_review(
            "review:\n  overall_assessment: ok\n  score: 70\n  risk_level: medium\n"
            f"  key_issues_to_review:\n{issues}"
            "  relevant_tests: No\n  security_concerns: No\n",
            num_max_findings=count,  # sidestep the model-side cap on purpose
        )

    def _run_with_inline_budget(self, count: int, budget: int) -> MagicMock:
        client = self._client_mock()
        # dense patch: lines 1..20 all exist in the diff, so the budget is the
        # only thing that can drop an issue from the inline output
        client.get_mr_changes.return_value = {
            "diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": "h"},
            "changes": [{"new_path": "app.py", "old_path": "app.py", "diff": DENSE_PATCH}],
        }
        review = self._many_issue_review(count)
        conf = _conf(inline_comments=True, inline_max_comments=budget, num_max_findings=count)
        with patch("reviewer.services.pipeline.GitLabClient", return_value=client), patch(
            "reviewer.services.pipeline.review_with_fallback",
            return_value=(review, "test-model"),
        ), patch("reviewer.services.pipeline.get_conf", return_value=conf):
            from .services.pipeline import run_review

            run_review("backend/payments", 1)
        return client

    def test_inline_comments_capped_by_configured_budget(self):
        client = self._run_with_inline_budget(count=12, budget=3)
        self.assertEqual(client.create_inline_discussion.call_count, 3)
        bodies = [call.args[-1] for call in client.create_note.call_args_list]
        summary = [b for b in bodies if REVIEW_MARKER in b and "AI Review" in b][0]
        self.assertIn("**1. Bug 3**", summary)  # the 4th issue onward stays in the summary
        self.assertNotIn("Bug 0", summary)
        self.assertEqual(ReviewRecord.objects.get().status, "success")

    def test_inline_budget_zero_falls_back_to_summary_only(self):
        client = self._run_with_inline_budget(count=3, budget=0)
        client.create_inline_discussion.assert_not_called()
        bodies = [call.args[-1] for call in client.create_note.call_args_list]
        summary = [b for b in bodies if REVIEW_MARKER in b and "AI Review" in b][0]
        self.assertIn("**1. Bug 0**", summary)

    def test_inline_budget_is_hard_capped_in_publish_stage(self):
        """Even a budget of 999 cannot exceed the code ceiling."""
        client = self._run_with_inline_budget(count=12, budget=999)
        self.assertEqual(client.create_inline_discussion.call_count, MAX_INLINE_COMMENTS)

    @staticmethod
    def _review_with_off_diff_issue() -> ReviewOutput:
        """One issue on a file with no diff, one on a real hunk line."""
        return parse_review(
            "review:\n  overall_assessment: ok\n  score: 70\n  risk_level: medium\n"
            "  key_issues_to_review:\n"
            "    - relevant_file: gen/models.py\n"
            "      issue_header: Bug\n"
            "      issue_content: file is not part of this MR\n"
            "      start_line: 5\n"
            "      end_line: 5\n"
            "    - relevant_file: app.py\n"
            "      issue_header: Bug\n"
            "      issue_content: real diff issue\n"
            "      start_line: 2\n"
            "      end_line: 2\n"
            "  relevant_tests: No\n  security_concerns: No\n",
            num_max_findings=6,
        )

    @staticmethod
    def _run_with_review(review: ReviewOutput, changes: list[dict]) -> MagicMock:
        client = MagicMock()
        client.get_merge_request.return_value = {
            "sha": "a" * 40,
            "web_url": "https://gl/g/r/-/merge_requests/1",
            "title": "Add feature",
            "description": "does things",
            "author": {"name": "alice"},
            "source_branch": "feat",
            "target_branch": "main",
            "merge_commit_message": "",
            "draft": False,
            "work_in_progress": False,
        }
        client.get_mr_changes.return_value = {
            "diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": "h"},
            "changes": changes,
        }
        client.get_own_user_id.return_value = 7
        client.list_notes.return_value = []
        client.list_discussions.return_value = []
        note_ids = iter([{"id": 55}, {"id": 66}, {"id": 77}])
        client.create_note.side_effect = lambda *a, **k: next(note_ids)
        with patch("reviewer.services.pipeline.GitLabClient", return_value=client), patch(
            "reviewer.services.pipeline.review_with_fallback", return_value=(review, "test-model")
        ):
            from .services.pipeline import run_review

            run_review("backend/payments", 1)
        return client

    @staticmethod
    def _summary_body(client: MagicMock) -> str:
        bodies = [call.args[-1] for call in client.create_note.call_args_list]
        return [b for b in bodies if REVIEW_MARKER in b and "AI Review" in b][0]

    def test_inline_comment_requires_file_with_diff(self):
        """An issue on a file the MR does not touch must never go inline."""
        client = self._run_with_review(
            self._review_with_off_diff_issue(),
            [{"new_path": "app.py", "old_path": "app.py", "diff": SAMPLE_PATCH}],
        )
        client.create_inline_discussion.assert_called_once()
        self.assertEqual(client.create_inline_discussion.call_args.args[3]["new_path"], "app.py")
        summary = self._summary_body(client)
        self.assertIn("file is not part of this MR", summary)  # off-diff issue falls back to summary
        self.assertNotIn("real diff issue", summary)
        self.assertEqual(ReviewRecord.objects.get().status, "success")

    def test_inline_comment_requires_line_inside_diff_hunks(self):
        """Line 7 only exists between the two hunks of SAMPLE_PATCH, not in the diff."""
        review = parse_review(
            "review:\n  overall_assessment: ok\n  score: 70\n  risk_level: low\n"
            "  key_issues_to_review:\n"
            "    - relevant_file: app.py\n"
            "      issue_header: Bug\n"
            "      issue_content: line lives between hunks\n"
            "      start_line: 7\n"
            "      end_line: 7\n"
            "  relevant_tests: No\n  security_concerns: No\n",
            num_max_findings=6,
        )
        client = self._run_with_review(
            review, [{"new_path": "app.py", "old_path": "app.py", "diff": SAMPLE_PATCH}]
        )
        client.create_inline_discussion.assert_not_called()
        self.assertIn("line lives between hunks", self._summary_body(client))


def _clear_db_config():
    Setting.objects.all().delete()
    RepoTokenEntry.objects.all().delete()
    reload_conf()


class ConfDbOverrideTests(TestCase):
    """Dashboard-saved settings must win over env defaults; blank means inherit."""

    def tearDown(self):
        _clear_db_config()

    def test_db_values_override_env(self):
        env = {"GITLABMR_MODEL": "env-model", "GITLABMR_NUM_MAX_FINDINGS": "8",
               "GITLABMR_INLINE_COMMENTS": "false", "GITLABMR_LLM_API_KEY": "env-key"}
        with patch.dict(os.environ, env):
            reload_conf()
            self.assertEqual(get_conf().model, "env-model")
            Setting.objects.create(model="db-model", num_max_findings=3, inline_comments=True)
            conf = reload_conf()
            self.assertEqual(conf.model, "db-model")
            self.assertEqual(conf.num_max_findings, 3)
            self.assertTrue(conf.inline_comments)
            self.assertEqual(conf.llm_api_key, "env-key")  # blank in DB -> inherit

    def test_blank_bool_keeps_env_value(self):
        with patch.dict(os.environ, {"GITLABMR_REVIEW_ON_PUSH": "false"}):
            reload_conf()
            self.assertFalse(get_conf().review_on_push)
            Setting.objects.create(review_on_push=None)
            self.assertFalse(reload_conf().review_on_push)
            Setting.objects.update(review_on_push=True)
            self.assertTrue(reload_conf().review_on_push)

    def test_db_repo_tokens_replace_env_ones(self):
        env = {"GITLABMR_GITLAB_TOKEN": "", "GITLABMR_WEBHOOK_SECRET": "",
               "GITLABMR_REPO_CONFIG": "", "GITLABMR_REPO_TOKENS": ""}
        with patch.dict(os.environ, env):
            reload_conf()
            self.assertEqual(get_conf().repo_tokens, ())
            RepoTokenEntry.objects.create(prefix="Backend/Team", token="glpat-db", api_url="https://db.example.com")
            conf = reload_conf()
            repo = resolve_repo_token(conf, "backend/team/svc")
            self.assertEqual(repo.token, "glpat-db")
            self.assertEqual(repo.api_url, "https://db.example.com")
            self.assertEqual(repo.prefix, "backend/team")
            self.assertIsNone(resolve_repo_token(conf, "other/repo"))
            # disabled entries drop out of the table
            RepoTokenEntry.objects.update(enabled=False)
            self.assertEqual(reload_conf().repo_tokens, ())

    def test_ttl_cache_reuses_conf_within_window(self):
        with patch.dict(os.environ, {"GITLABMR_MODEL": "cached-model"}):
            reload_conf()
            Setting.objects.create(model="fresh-model")
            self.assertEqual(get_conf().model, "cached-model")  # within TTL
            reload_conf()
            self.assertEqual(get_conf().model, "fresh-model")  # forced refresh

    def test_env_cannot_exceed_inline_and_findings_ceilings(self):
        with patch.dict(os.environ, {"GITLABMR_INLINE_MAX_COMMENTS": "999",
                                    "GITLABMR_NUM_MAX_FINDINGS": "999"}):
            conf = reload_conf()
        self.assertEqual(conf.inline_max_comments, MAX_INLINE_COMMENTS)
        self.assertEqual(conf.num_max_findings, MAX_FINDINGS)

    def test_context_enrichment_env_knobs(self):
        with patch.dict(os.environ, {"GITLABMR_CONTEXT_ENRICHMENT": "false",
                                     "GITLABMR_CONTEXT_MAX_FILES": "3",
                                     "GITLABMR_CONTEXT_MAX_FILE_CHARS": "5000",
                                     "GITLABMR_CONTEXT_MAX_SYMBOLS": "2"}):
            conf = reload_conf()
        self.assertFalse(conf.context_enrichment)
        self.assertEqual((conf.context_max_files, conf.context_max_file_chars, conf.context_max_symbols), (3, 5000, 2))

    def test_db_cannot_exceed_inline_and_findings_ceilings(self):
        Setting.objects.create(inline_max_comments=500, num_max_findings=100)
        conf = reload_conf()
        self.assertEqual(conf.inline_max_comments, MAX_INLINE_COMMENTS)
        self.assertEqual(conf.num_max_findings, MAX_FINDINGS)

    def test_inline_budget_zero_disables_inline(self):
        Setting.objects.create(inline_max_comments=0)
        self.assertEqual(reload_conf().inline_max_comments, 0)


class UiAuthTests(TestCase):
    def test_open_access_without_password(self):
        with patch.dict(os.environ, {"GITLABMR_UI_PASSWORD": ""}):
            self.assertEqual(self.client.get("/").status_code, 200)
            self.assertEqual(self.client.get("/settings/llm/").status_code, 200)
            self.assertEqual(self.client.get("/settings/repos/").status_code, 200)

    def test_login_flow(self):
        with patch.dict(os.environ, {"GITLABMR_UI_PASSWORD": "secret123"}):
            resp = self.client.get("/")
            self.assertRedirects(resp, "/login/?next=/")
            self.assertContains(self.client.get("/login/"), "访问密码")
            resp = self.client.post("/login/", {"password": "wrong", "next": "/"})
            self.assertEqual(resp.status_code, 200)  # re-renders with error
            self.assertRedirects(self.client.post("/login/", {"password": "secret123", "next": "/"}), "/")
            self.assertEqual(self.client.get("/").status_code, 200)
            self.assertRedirects(self.client.post("/logout/"), "/login/")
            self.assertRedirects(self.client.get("/"), "/login/?next=/")

    def test_open_redirect_blocked(self):
        with patch.dict(os.environ, {"GITLABMR_UI_PASSWORD": "p"}):
            resp = self.client.post("/login/", {"password": "p", "next": "//evil.example.com"})
            self.assertRedirects(resp, "/")


class MRRefTests(TestCase):
    def test_parses_mr_page_url(self):
        ref = parse_mr_ref(
            "https://gitlab.inone.nsfocus.com/tp/CNEOSv2/OS/kernel/linux-5.4.18-kylinOS/-/merge_requests/118"
        )
        self.assertEqual(ref.project_path, "tp/CNEOSv2/OS/kernel/linux-5.4.18-kylinOS")
        self.assertEqual(ref.iid, 118)
        self.assertEqual(ref.host, "gitlab.inone.nsfocus.com")
        self.assertEqual(ref.short, "tp/CNEOSv2/OS/kernel/linux-5.4.18-kylinOS!118")

    def test_url_variants(self):
        cases = {
            "https://gl.example.com/a/b/-/merge_requests/7/diffs?tab=x#note_1": ("a/b", 7, "gl.example.com"),
            "https://gl.example.com:8443/a/b/merge_requests/7": ("a/b", 7, "gl.example.com"),
            "gl.example.com/a/b/-/merge_requests/7": ("a/b", 7, "gl.example.com"),
            "a/b/-/merge_requests/7": ("a/b", 7, ""),
            "  https://gl.example.com/a/b/-/merge_requests/7  ": ("a/b", 7, "gl.example.com"),
        }
        for raw, (project, iid, host) in cases.items():
            ref = parse_mr_ref(raw)
            self.assertEqual((ref.project_path, ref.iid, ref.host), (project, iid, host), raw)

    def test_shortcut_forms(self):
        for raw in ("group/sub/repo!42", "group/sub/repo 42", "group/sub/repo#42"):
            ref = parse_mr_ref(raw)
            self.assertEqual((ref.project_path, ref.iid, ref.host), ("group/sub/repo", 42, ""), raw)

    def test_rejects_unusable_input(self):
        for raw in ("", "group/repo", "!42", "group/repo!abc", "https://gl.example.com/-/merge_requests/12"):
            with self.assertRaises(MRRefError, msg=raw):
                parse_mr_ref(raw)


class DashboardTests(TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"GITLABMR_UI_PASSWORD": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _record(self, **kw):
        defaults = dict(
            project_path="backend/payments", mr_iid=7, head_sha="a" * 40, status="success",
            score=88, risk_level="low", issues_count=2, model_used="m1",
            mr_url="https://gl/backend/payments/-/merge_requests/7",
        )
        defaults.update(kw)
        return ReviewRecord.objects.create(**defaults)

    def test_dashboard_lists_reviews_and_events(self):
        self._record()
        WebhookEvent.objects.create(object_kind="merge_request", project_path="backend/payments",
                                    action="open", handled="review")
        resp = self.client.get("/")
        self.assertContains(resp, "backend/payments")
        self.assertContains(resp, "88")
        self.assertContains(resp, "review")

    def test_trigger_review_from_mr_url(self):
        url = "https://gitlab.example.com/backend/payments/-/merge_requests/7"
        with patch("reviewer.ui.get_conf", return_value=_conf()), patch("reviewer.ui.submit_review") as submit:
            resp = self.client.post("/review/trigger/", {"mr_ref": url})
            self.assertRedirects(resp, "/")
            submit.assert_called_once_with("backend/payments", 7, source="manual")

    def test_trigger_review_keeps_project_casing(self):
        # webhook payloads keep the original path casing -> same review identity
        url = "https://gitlab.example.com/Backend/Payments/-/merge_requests/7"
        with patch("reviewer.ui.get_conf", return_value=_conf()), patch("reviewer.ui.submit_review") as submit:
            self.client.post("/review/trigger/", {"mr_ref": url})
        submit.assert_called_once_with("Backend/Payments", 7, source="manual")

    def test_trigger_review_legacy_two_fields(self):
        with patch("reviewer.ui.get_conf", return_value=_conf()), patch("reviewer.ui.submit_review") as submit:
            self.client.post("/review/trigger/", {"project": "backend/payments", "mr_iid": "7"})
        submit.assert_called_once_with("backend/payments", 7, source="manual")

    def test_trigger_review_rejects_bad_input(self):
        with patch("reviewer.ui.get_conf", return_value=_conf()), patch("reviewer.ui.submit_review") as submit:
            for payload in ({"mr_ref": "nonsense"}, {"mr_ref": ""}):
                resp = self.client.post("/review/trigger/", payload)
                self.assertRedirects(resp, "/")
        submit.assert_not_called()

    def test_trigger_review_host_mismatch_blocked(self):
        url = "https://gitlab.other.example.com/backend/payments/-/merge_requests/7"
        with patch("reviewer.ui.get_conf", return_value=_conf()), patch("reviewer.ui.submit_review") as submit:
            self.client.post("/review/trigger/", {"mr_ref": url})
        submit.assert_not_called()  # wrong instance: would review the wrong repo

    def test_trigger_review_without_token_blocked(self):
        url = "https://gitlab.example.com/unknown/repo/-/merge_requests/7"
        with patch("reviewer.ui.get_conf", return_value=_conf(default_token="")), \
                patch("reviewer.ui.submit_review") as submit:
            self.client.post("/review/trigger/", {"mr_ref": url})
        submit.assert_not_called()

    def test_detail_and_retry(self):
        record = self._record(status="failed", error_reason="http_error")
        self.assertContains(self.client.get(f"/reviews/{record.pk}/"), "http_error")
        with patch("reviewer.ui.submit_review") as submit:
            self.client.post(f"/reviews/{record.pk}/retry/")
        submit.assert_called_once_with("backend/payments", 7, source="manual", force=True)

    def test_detail_shows_skip_reason(self):
        record = self._record(status="skipped", error_detail="no reviewable files")
        resp = self.client.get(f"/reviews/{record.pk}/")
        self.assertContains(resp, "no reviewable files")
        self.assertContains(resp, "跳过原因")

    def test_retry_allowed_for_success(self):
        record = self._record(status="success")
        with patch("reviewer.ui.submit_review") as submit:
            resp = self.client.post(f"/reviews/{record.pk}/retry/")
        submit.assert_called_once_with(record.project_path, record.mr_iid, source="manual", force=True)
        self.assertEqual(resp.status_code, 302)


class SettingsViewTests(TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"GITLABMR_UI_PASSWORD": "", "GITLABMR_MODEL": "env-model",
                                          "GITLABMR_LLM_API_KEY": "env-key", "GITLABMR_WEBHOOK_SECRET": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        _clear_db_config()

    def test_settings_page_renders_effective_values(self):
        reload_conf()
        resp = self.client.get("/settings/llm/")
        self.assertContains(resp, "env-model")
        self.assertNotContains(resp, "env-key")  # secrets are never echoed back

    def test_save_settings_overrides_conf(self):
        resp = self.client.post("/settings/llm/", {
            "action": "save_settings", "model": "db-model", "num_max_findings": "4",
            "inline_comments": "true", "review_on_push": "", "llm_api_key": "",
            "workers": "not-a-number",
        })
        self.assertRedirects(resp, "/settings/llm/")
        self.client.post("/settings/repos/", {"action": "save_settings", "webhook_secret": "whsec-new"})
        conf = reload_conf()
        self.assertEqual(conf.model, "db-model")
        self.assertEqual(conf.num_max_findings, 4)
        self.assertTrue(conf.inline_comments)
        self.assertTrue(conf.review_on_push)  # inherit -> env default true
        self.assertEqual(conf.llm_api_key, "env-key")  # secret blank = keep
        self.assertEqual(conf.webhook_secret, "whsec-new")
        self.assertNotEqual(conf.workers, "not-a-number")

    def test_second_save_updates_same_row(self):
        self.client.post("/settings/llm/", {"action": "save_settings", "model": "db-model", "llm_api_key": "sk-1"})
        self.client.post("/settings/llm/", {"action": "save_settings", "model": "db-model", "llm_api_key": "sk-2"})
        conf = reload_conf()
        self.assertEqual(Setting.objects.count(), 1)
        self.assertEqual(conf.model, "db-model")
        self.assertEqual(conf.llm_api_key, "sk-2")

    def test_repo_form_collapsed_until_requested(self):
        RepoTokenEntry.objects.create(prefix="backend/team", token="glpat-db")
        # plain page: only the table + an "add" entry point, no add/edit form
        resp = self.client.get("/settings/repos/")
        self.assertContains(resp, "多仓库前缀令牌")
        self.assertContains(resp, "新增前缀")
        self.assertNotContains(resp, 'id="repo-form"')
        self.assertNotContains(resp, "用途说明")  # field unique to the add/edit form
        # "编辑" opens the form, prefilled from the stored entry; prefix stays editable (rename)
        resp = self.client.get("/settings/repos/?edit=backend/team")
        self.assertContains(resp, 'id="repo-form"')
        self.assertContains(resp, 'name="prefix" value="backend/team"')
        self.assertNotContains(resp, "readonly")
        self.assertContains(resp, 'name="entry_id" value="')
        self.assertContains(resp, "保存修改")
        self.assertNotContains(resp, "新增前缀")
        # "新增" opens the same form empty, prefix editable
        resp = self.client.get("/settings/repos/?new=1")
        self.assertContains(resp, 'id="repo-form"')
        self.assertContains(resp, 'name="prefix" value=""')
        self.assertNotContains(resp, "readonly")
        self.assertContains(resp, "添加 / 更新前缀")

    def test_repo_token_add_update_delete(self):
        resp = self.client.post("/settings/repos/", {
            "action": "save_repo", "prefix": "Backend/Team", "token": "glpat-1",
            "api_url": "https://gl.example.com/", "webhook_secret": "", "note": "team repo", "enabled": "on",
        })
        self.assertRedirects(resp, "/settings/repos/")
        entry = RepoTokenEntry.objects.get()
        self.assertEqual((entry.prefix, entry.token, entry.api_url), ("backend/team", "glpat-1", "https://gl.example.com"))
        repo = resolve_repo_token(reload_conf(), "backend/team/svc")
        self.assertEqual(repo.token, "glpat-1")
        # update same prefix, blank token keeps stored one
        self.client.post("/settings/repos/", {"action": "save_repo", "prefix": "backend/team", "token": "",
                                              "api_url": "https://other.example.com", "enabled": "on"})
        entry.refresh_from_db()
        self.assertEqual((entry.token, entry.api_url), ("glpat-1", "https://other.example.com"))
        # new prefix requires a token
        self.client.post("/settings/repos/", {"action": "save_repo", "prefix": "new/prefix", "token": ""})
        self.assertEqual(RepoTokenEntry.objects.count(), 1)
        # delete
        self.client.post("/settings/repos/", {"action": "delete_repo", "prefix": "backend/team"})
        self.assertEqual(RepoTokenEntry.objects.count(), 0)
        self.assertEqual(resolve_repo_token(reload_conf(), "backend/team/svc"), None)

    def test_repo_token_rename_by_id(self):
        entry = RepoTokenEntry.objects.create(prefix="backend/team", token="glpat-1", api_url="https://a.example.com")
        # 编辑态按主键更新：改前缀 = 重命名，令牌留空保持不变
        resp = self.client.post("/settings/repos/", {
            "action": "save_repo", "entry_id": str(entry.pk), "prefix": "Backend/Team-A", "token": "",
            "api_url": "https://a.example.com", "enabled": "on",
        })
        self.assertRedirects(resp, "/settings/repos/")
        self.assertEqual(RepoTokenEntry.objects.count(), 1)
        entry.refresh_from_db()
        self.assertEqual((entry.prefix, entry.token), ("backend/team-a", "glpat-1"))
        conf = reload_conf()
        self.assertEqual(resolve_repo_token(conf, "backend/team-a/svc").token, "glpat-1")
        self.assertIsNone(resolve_repo_token(conf, "backend/team/svc"))

    def test_repo_token_rename_conflict_rejected(self):
        a = RepoTokenEntry.objects.create(prefix="backend/team", token="glpat-1")
        RepoTokenEntry.objects.create(prefix="backend/other", token="glpat-2")
        self.client.post("/settings/repos/", {
            "action": "save_repo", "entry_id": str(a.pk), "prefix": "backend/other", "token": "", "enabled": "on",
        })
        a.refresh_from_db()
        self.assertEqual(a.prefix, "backend/team")  # 冲突时不改名
        self.assertEqual(RepoTokenEntry.objects.count(), 2)
        self.assertEqual(a.token, "glpat-1")  # 令牌也不会被覆盖
