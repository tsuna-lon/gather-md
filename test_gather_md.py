"""
test_watch_report.py
watch_report.py の単体テスト
"""

import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import unittest

from gather_md import (
    load_project_config,
    find_project_config_for_file,
    extract_blocks,
    calculate_character_counts,
    generate_report,
    rollback_report,
    create_default_template_if_needed,
    set_file_writable,
    set_file_readonly,
    WARNING_BANNER,
)


class TestWatchReport(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        # 読み取り専用ファイルが存在しても安全に削除できるように書き込み権限を付与
        for p in self.test_dir.glob("**/*"):
            if p.is_file():
                try:
                    os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
                except Exception:
                    pass
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
        self.assertIn('readonly: false', content)
        self.assertIn('warning_banner: true', content)
        self.assertIn('auto_rollback: true', content)
        self.assertIn('{{１．はじめに}}', content)
        self.assertIn('{{char_count}}', content)

        # 初期レポートも生成されていること
        output_file = new_proj / "_完成レポート.md"
        self.assertTrue(output_file.is_file())
        # レポートに警告バナーが含まれていること
        out_content = output_file.read_text(encoding="utf-8")
        self.assertIn(WARNING_BANNER.strip(), out_content)

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

    def test_prevent_edit_readonly_and_banner(self):
        """誤編集防止機能: 警告バナーが付与され、ファイルが読み取り専用になること"""
        proj_dir = self.test_dir / "project_prevent_edit"
        proj_dir.mkdir()
        config_path = proj_dir / "report_config.yaml"
        config_path.write_text(
            """
output: "out.md"
readonly: true
warning_banner: true
template: |
  # レポート本文
  {{sec1}}
""",
            encoding="utf-8",
        )
        draft = proj_dir / "draft.md"
        draft.write_text("```report: sec1\n本文です。\n```", encoding="utf-8")

        cfg = load_project_config(proj_dir)
        self.assertIsNotNone(cfg)
        self.assertTrue(cfg.readonly)
        self.assertTrue(cfg.warning_banner)

        # レポート生成
        res = generate_report(cfg)
        self.assertTrue(res)
        self.assertTrue(cfg.output_file.is_file())

        # 1. 警告バナーが先頭に含まれていること
        content = cfg.output_file.read_text(encoding="utf-8")
        self.assertTrue(content.startswith(WARNING_BANNER))
        self.assertIn("本文です。", content)

        # 2. ファイルが読み取り専用（書き込み不可）になっていること
        with self.assertRaises(PermissionError):
            with open(cfg.output_file, "w", encoding="utf-8") as f:
                f.write("手動で上書きしようとする")

        # 3. 再度 generate_report を実行しても正常に上書きできること
        draft.write_text("```report: sec1\n更新された本文です。\n```", encoding="utf-8")
        res_update = generate_report(cfg)
        self.assertTrue(res_update)
        updated_content = cfg.output_file.read_text(encoding="utf-8")
        self.assertIn("更新された本文です。", updated_content)

    def test_warning_banner_excluded_from_character_counts(self):
        """警告バナーの文字が文字数集計（char_count）に含まれないこと"""
        proj_dir = self.test_dir / "project_count"
        proj_dir.mkdir()
        config_path = proj_dir / "report_config.yaml"
        draft = proj_dir / "draft.md"
        draft.write_text("```report: sec\nあいうえお\n```", encoding="utf-8")

        # 1. バナーありで生成
        config_path.write_text(
            """
output: "out.md"
readonly: false
warning_banner: true
template: |
  # 題名
  {{sec}}
  文字数: {{char_count_raw}}
""",
            encoding="utf-8",
        )
        cfg_with = load_project_config(proj_dir)
        generate_report(cfg_with)
        content_with = cfg_with.output_file.read_text(encoding="utf-8")
        self.assertIn(WARNING_BANNER.strip(), content_with)
        count_with = re.search(r"文字数:\s*(\d+)", content_with).group(1)

        # 2. バナーなしで生成
        config_path.write_text(
            """
output: "out.md"
readonly: false
warning_banner: false
template: |
  # 題名
  {{sec}}
  文字数: {{char_count_raw}}
""",
            encoding="utf-8",
        )
        cfg_without = load_project_config(proj_dir)
        generate_report(cfg_without)
        content_without = cfg_without.output_file.read_text(encoding="utf-8")
        self.assertNotIn(WARNING_BANNER.strip(), content_without)
        count_without = re.search(r"文字数:\s*(\d+)", content_without).group(1)

        # バナーの有無に関係なく、本文の集計文字数が完全に一致すること
        self.assertEqual(count_with, count_without)

    def test_disable_readonly_and_banner(self):
        """readonly: false, warning_banner: false で保護機能を無効化できること"""
        proj_dir = self.test_dir / "project_disabled"
        proj_dir.mkdir()
        config_path = proj_dir / "report_config.yaml"
        config_path.write_text(
            """
output: "out.md"
readonly: false
warning_banner: false
template: |
  # タイトル
  {{sec}}
""",
            encoding="utf-8",
        )
        draft = proj_dir / "draft.md"
        draft.write_text("```report: sec\n本文\n```", encoding="utf-8")

        cfg = load_project_config(proj_dir)
        self.assertFalse(cfg.readonly)
        self.assertFalse(cfg.warning_banner)

        generate_report(cfg)

        content = cfg.output_file.read_text(encoding="utf-8")
        # バナーが付与されていないこと
        self.assertNotIn(WARNING_BANNER.strip(), content)

        # ファイルが通常通り書き込み可能であること
        try:
            with open(cfg.output_file, "a", encoding="utf-8") as f:
                f.write("\n手動追記")
        except PermissionError:
            self.fail("readonly: false なのに PermissionError が発生しました")

    def test_auto_rollback_on_direct_edit(self):
        """完成レポートが直接編集された場合、下書きから自動復元（ロールバック）されること"""
        proj_dir = self.test_dir / "project_rollback"
        proj_dir.mkdir()
        config_path = proj_dir / "_template.md"
        config_path.write_text(
            """---
output: "_完成レポート.md"
readonly: false
warning_banner: true
auto_rollback: true
---
# 課題レポート
{{sec1}}
""",
            encoding="utf-8",
        )
        draft = proj_dir / "draft.md"
        draft.write_text("```report: sec1\n正規の下書き本文です。\n```", encoding="utf-8")

        cfg = load_project_config(proj_dir)
        self.assertIsNotNone(cfg)
        self.assertTrue(cfg.auto_rollback)

        # 1. 正常なレポート生成
        self.assertTrue(generate_report(cfg))
        original_content = cfg.output_file.read_text(encoding="utf-8")
        self.assertIn("正規の下書き本文です。", original_content)

        # 2. 外部で直接編集（誤編集）を実行
        with open(cfg.output_file, "w", encoding="utf-8") as f:
            f.write("# 誤って直接編集した内容")

        # 3. ロールバック実行
        rolled_back = rollback_report(cfg)
        self.assertTrue(rolled_back)

        # 4. 内容が正規の内容に復元されていること
        restored_content = cfg.output_file.read_text(encoding="utf-8")
        self.assertEqual(restored_content, original_content)
        self.assertNotIn("誤って直接編集した内容", restored_content)

        # 5. 差分がない状態で再度ロールバックを呼んだ場合は False となり不要な再書き込みが行われないこと
        self.assertFalse(rollback_report(cfg))

    def test_unlock_existing_readonly_file(self):
        """既存ファイルが読み取り専用になっていても、readonly: false で自動的に書き込み可能に解除されること"""
        proj_dir = self.test_dir / "project_unlock"
        proj_dir.mkdir()
        out_file = proj_dir / "_完成レポート.md"
        out_file.write_text("古い内容", encoding="utf-8")
        set_file_readonly(out_file)

        # 読み取り専用になっていることを確認
        with self.assertRaises(PermissionError):
            with open(out_file, "w", encoding="utf-8") as f:
                f.write("書き込めないはず")

        # readonly: false の設定でレポート生成を実行
        config_path = proj_dir / "_template.md"
        config_path.write_text(
            """---
output: "_完成レポート.md"
readonly: false
---
# 新しいレポート
""",
            encoding="utf-8",
        )
        cfg = load_project_config(proj_dir)
        self.assertIsNotNone(cfg)
        self.assertFalse(cfg.readonly)

        # 生成により上書きされ、読み取り専用属性が解除される
        self.assertTrue(generate_report(cfg))

        # 書き込み権限が回復していること
        try:
            with open(out_file, "a", encoding="utf-8") as f:
                f.write("\n追記可能")
        except PermissionError:
            self.fail("読み取り専用属性が解除されていません")


if __name__ == "__main__":
    unittest.main()
