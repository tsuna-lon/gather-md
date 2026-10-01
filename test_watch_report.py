"""
test_watch_report.py
watch_report.py の単体テスト
"""

import tempfile
import shutil
from pathlib import Path
import unittest

from gather_md import (
    load_project_config,
    find_project_config_for_file,
    extract_blocks,
    calculate_character_counts,
    generate_report,
)


class TestWatchReport(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_yaml_config_loading(self):
        proj_dir = self.test_dir / "project1"
        proj_dir.mkdir()
        config_path = proj_dir / "report_config.yaml"
        config_path.write_text(
            """
output: "out.md"
template: |
  # タイトル
  {{intro}}
  {{body}}
""",
            encoding="utf-8",
        )

        cfg = load_project_config(proj_dir)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.output_file.name, "out.md")
        self.assertIn("# タイトル", cfg.template_content)

    def test_template_md_loading(self):
        proj_dir = self.test_dir / "project2"
        proj_dir.mkdir()
        template_path = proj_dir / "_template.md"
        template_path.write_text(
            """---
output: "custom_report.md"
---
# テンプレートタイトル
<!-- INSERT:chap1 -->
<!-- INSERT:chap2 -->
""",
            encoding="utf-8",
        )

        cfg = load_project_config(proj_dir)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.output_file.name, "custom_report.md")
        self.assertIn("# テンプレートタイトル", cfg.template_content)
        self.assertNotIn("output: custom_report.md", cfg.template_content)

    def test_scope_resolution_from_subfolder(self):
        proj_dir = self.test_dir / "project3"
        sub_dir = proj_dir / "下書き" / "章1"
        sub_dir.mkdir(parents=True)

        config_path = proj_dir / "report_config.yaml"
        config_path.write_text("output: 'out.md'\ntemplate: '{{tag}}'", encoding="utf-8")

        draft_file = sub_dir / "draft.md"
        draft_file.write_text("```report: tag\n本文\n```", encoding="utf-8")

        # sub_dir 内のファイルから親の proj_dir の設定が拾えるか（root_dir に test_dir を指定）
        cfg = find_project_config_for_file(draft_file, root_dir=self.test_dir)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.project_dir.resolve(), proj_dir.resolve())

    def test_extract_blocks_and_generate(self):
        proj_dir = self.test_dir / "project4"
        sub_dir = proj_dir / "下書き"
        sub_dir.mkdir(parents=True)

        # 設定ファイル
        config_path = proj_dir / "report_config.yaml"
        config_path.write_text(
            """
output: "_完成レポート.md"
template: |
  # レポート
  <!-- INSERT: １．はじめに -->
  <!-- END: １．はじめに -->
  {{２．本文}}
  {{未作成の章}}
  
  (文字数: {{char_count}}字 / 空白込: {{char_count_with_spaces}}字)
""",
            encoding="utf-8",
        )

        # 下書き1 (全角数字・空白入りタグ)
        draft1 = sub_dir / "1_はじめに.md"
        draft1.write_text(
            """
メモや資料のURLなど。
```report: １．はじめに
これは「はじめに」の本文です。
```
参考URLなど。
""",
            encoding="utf-8",
        )

        # 下書き2
        draft2 = sub_dir / "2_本文.md"
        draft2.write_text(
            """
```report:２．本文
これは第2章の本文です。
```
""",
            encoding="utf-8",
        )

        cfg = load_project_config(proj_dir)
        self.assertIsNotNone(cfg)

        success = generate_report(cfg)
        self.assertTrue(success)

        out_content = cfg.output_file.read_text(encoding="utf-8")
        # １．はじめにが展開されているか
        self.assertIn("これは「はじめに」の本文です。", out_content)
        # ２．本文が展開されているか
        self.assertIn("これは第2章の本文です。", out_content)
        # 未作成の章が注記になっているか
        self.assertIn("【未作成: 未作成の章】", out_content)
        # 文字数カウントが入っているか
        self.assertRegex(out_content, r"\(文字数: \d+字 / 空白込: \d+字\)")
        print("\n--- 生成結果プレビュー ---\n" + out_content)


if __name__ == "__main__":
    unittest.main()
