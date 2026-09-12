from scripts.check_markdown_links import broken_links


def test_standard_external_and_valid_local_links_are_accepted(tmp_path):
    (tmp_path / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "[GitHub](https://github.com/example)\n"
        "[LinkedIn](https://linkedin.com/in/example)\n"
        "[Guide](guide.md)\n",
        encoding="utf-8",
    )

    assert broken_links(tmp_path) == []


def test_broken_local_and_nonstandard_url_targets_are_detected(tmp_path):
    (tmp_path / "README.md").write_text(
        "[Missing](missing.md)\n"
        "[Nonstandard](@url:`https://github.com/example`)\n",
        encoding="utf-8",
    )

    assert broken_links(tmp_path) == [
        ("README.md", "missing.md"),
        ("README.md", "@url:`https://github.com/example`"),
    ]
