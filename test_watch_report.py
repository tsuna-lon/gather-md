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
    create_default_template_if_needed,
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

    def test_create_default_template_on_root_dir(self):
        """直下の新規ディレクトリ作成時にテンプレートが自動生成されること"""
        new_proj = self.test_dir / "課題1 - 情報社会論"
        new_proj.mkdir()

        res = create_default_template_if_needed(new_proj, root_dir=self.test_dir)
        self.assertIsNotNone(res)
        template_file = new_proj / "_template.md"
        self.assertTrue(template_file.is_file())

        content = template_file.read_text(encoding="utf-8")
        self.assertIn('# 課題1 - 情報社会論 レポート', content)
        self.assertIn('output: "_完成レポート.md"', content)
        self.assertIn('<!-- INSERT:１．はじめに -->', content)
        self.assertIn('{{char_count}}', content)

        # 初期レポートも生成されていること
        output_file = new_proj / "_完成レポート.md"
        self.assertTrue(output_file.is_file())

    def test_ignore_subdirectory(self):
        """サブディレクトリ（2階層目以降）が作成された場合はテンプレート生成を行わないこと"""
        parent_proj = self.test_dir / "課題2"
        parent_proj.mkdir()
        # 親にはテンプレートを作成
        create_default_template_if_needed(parent_proj, root_dir=self.test_dir)

        # サブディレクトリを作成
        sub_dir = parent_proj / "下書き_第1章"
        sub_dir.mkdir()

        res = create_default_template_if_needed(sub_dir, root_dir=self.test_dir)
        self.assertIsNone(res)
        self.assertFalse((sub_dir / "_template.md").exists())

    def test_ignore_hidden_and_cache_directories(self):
        """隠しフォルダやキャッシュフォルダは無視すること"""
        obsidian_dir = self.test_dir / ".obsidian"
        obsidian_dir.mkdir()
        res1 = create_default_template_if_needed(obsidian_dir, root_dir=self.test_dir)
        self.assertIsNone(res1)
        self.assertFalse((obsidian_dir / "_template.md").exists())

        pycache_dir = self.test_dir / "__pycache__"
        pycache_dir.mkdir()
        res2 = create_default_template_if_needed(pycache_dir, root_dir=self.test_dir)
        self.assertIsNone(res2)
        self.assertFalse((pycache_dir / "_template.md").exists())

    def test_hold_temporary_windows_folder_name(self):
        """「新しいフォルダー」等のWindows初期名称では保留され、リネーム後に生成されること"""
        temp_dir = self.test_dir / "新しいフォルダー"
        temp_dir.mkdir()

        # 一時名の間は保留される
        res = create_default_template_if_needed(temp_dir, root_dir=self.test_dir)
        self.assertIsNone(res)
        self.assertFalse((temp_dir / "_template.md").exists())

        # リネーム後
        renamed_dir = self.test_dir / "課題3 - 経済学"
        temp_dir.rename(renamed_dir)

        res_renamed = create_default_template_if_needed(renamed_dir, root_dir=self.test_dir)
        self.assertIsNotNone(res_renamed)
        self.assertTrue((renamed_dir / "_template.md").exists())

    def test_do_not_overwrite_existing_template_or_config(self):
        """既存のテンプレートや設定ファイルが存在する場合は上書きしないこと"""
        custom_proj = self.test_dir / "課題4"
        custom_proj.mkdir()
        existing_template = custom_proj / "_template.md"
        existing_template.write_text("カスタムテンプレート", encoding="utf-8")

        res = create_default_template_if_needed(custom_proj, root_dir=self.test_dir)
        self.assertIsNone(res)
        self.assertEqual(existing_template.read_text(encoding="utf-8"), "カスタムテンプレート")


if __name__ == "__main__":
    unittest.main()
