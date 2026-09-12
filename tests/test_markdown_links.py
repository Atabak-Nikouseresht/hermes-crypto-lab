from scripts.check_markdown_links import broken_links


def test_url_attachment_targets_are_not_local_links(tmp_path):
    (tmp_path / "README.md").write_text(
        "GitHub: [Atabak-Nikouseresht](@url:`https://github.com/Atabak-Nikouseresht`)\n"
        "LinkedIn: [Atabak Nikouseresht](@url:`https://linkedin.com/in/atabak-nikouseresht`)\n",
        encoding="utf-8",
    )

    assert broken_links(tmp_path) == []
