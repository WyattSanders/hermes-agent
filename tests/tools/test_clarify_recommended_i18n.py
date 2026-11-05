"""The clarify "(Recommended)" label follows ``display.language``.

Regression for #149021: the label was a hardcoded English constant, so a French prompt came out
French on every line except the recommended choice.
"""

import json

import pytest

from agent import i18n
from tools import clarify_tool as ct


@pytest.fixture(autouse=True)
def _fresh_language_cache(monkeypatch):
    monkeypatch.delenv("HERMES_LANGUAGE", raising=False)
    i18n.reset_language_cache()
    yield
    i18n.reset_language_cache()


@pytest.fixture
def french(monkeypatch, tmp_path):
    """``display.language: fr`` in an isolated profile home."""
    (tmp_path / "config.yaml").write_text("display:\n  language: fr\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    i18n.reset_language_cache()
    assert i18n.get_language() == "fr"


def _ask_first_choice(choices):
    seen = {}

    def cb(questions):
        seen["choices"] = list(questions[0]["choices"])
        return {"answers": {"q0": questions[0]["choices"][0]}, "outcome": "submitted"}

    result = json.loads(ct.clarify_tool([{"question": "Pick", "choices": choices}], callback=cb))
    return seen["choices"], result["responses"][0]["user_response"]


def test_label_is_localized_under_display_language_fr(french):
    shown, answer = _ask_first_choice(["Rebase", "Merge"])
    assert shown == ["Rebase (recommandé)", "Merge"]
    assert answer == "Rebase"


def test_mark_recommended_is_idempotent_in_both_languages(french):
    once = ct.mark_recommended(["Rebase", "Merge"])
    assert ct.mark_recommended(once) == once
    english = ["Rebase (Recommended)", "Merge"]
    assert ct.mark_recommended(english) == english  # an English-labelled choice is not relabelled


@pytest.mark.parametrize("text", [
    "Rebase (recommandé)", "Rebase (RECOMMANDÉ)", "Rebase (Recommended)", "Rebase (recommended)",
    "  Rebase   (Recommandé)  ",
])
def test_strip_removes_active_and_english_labels_case_insensitively(french, text):
    assert ct.strip_recommended(text) == "Rebase"


def test_english_label_still_stripped_after_language_change_to_fr(french):
    """An answer copied before the switch (English label) comes back clean."""
    def cb(questions):
        return {"answers": {"q0": "Rebase (Recommended)"}, "outcome": "submitted"}

    result = json.loads(ct.clarify_tool([{"question": "Pick", "choices": ["Rebase", "Merge"]}], callback=cb))
    assert result["responses"][0]["user_response"] == "Rebase"


def test_locale_without_the_key_falls_back_to_english(monkeypatch, tmp_path):
    locales = tmp_path / "locales"
    locales.mkdir()
    (locales / "en.yaml").write_text("gateway:\n  clarify:\n    recommended: '(Recommended)'\n", encoding="utf-8")
    (locales / "fr.yaml").write_text("gateway:\n  clarify:\n    hint_single: 'Répondez.'\n", encoding="utf-8")
    monkeypatch.setattr(i18n, "_locales_dir", lambda: locales)
    monkeypatch.setenv("HERMES_LANGUAGE", "fr")
    i18n.reset_language_cache()
    assert ct.mark_recommended(["Rebase", "Merge"])[0] == "Rebase (Recommended)"


def test_catalog_with_no_key_anywhere_keeps_the_english_label(monkeypatch, tmp_path):
    """t() returns the bare key on a total miss; the label must never become that key or empty."""
    locales = tmp_path / "locales"
    locales.mkdir()
    (locales / "en.yaml").write_text("gateway:\n  clarify:\n    hint_single: 'Reply.'\n", encoding="utf-8")
    monkeypatch.setattr(i18n, "_locales_dir", lambda: locales)
    i18n.reset_language_cache()
    assert ct.mark_recommended(["Rebase", "Merge"])[0] == "Rebase (Recommended)"


def test_label_degrades_to_english_when_translation_raises(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("i18n unavailable")

    monkeypatch.setattr(i18n, "t", boom)
    assert ct.mark_recommended(["Rebase", "Merge"])[0] == "Rebase (Recommended)"
    assert ct.strip_recommended("Rebase (Recommended)") == "Rebase"


@pytest.mark.parametrize("lang", [l for l in i18n.SUPPORTED_LANGUAGES if l != "en"])
def test_every_bundled_locale_with_clarify_hints_translates_the_label(monkeypatch, lang):
    """No locale that localizes the clarify hints is left with the English label."""
    assert i18n.t("gateway.clarify.hint_single", lang=lang) != i18n.t("gateway.clarify.hint_single", lang="en")
    monkeypatch.setenv("HERMES_LANGUAGE", lang)
    i18n.reset_language_cache()
    marked = ct.mark_recommended(["Rebase", "Merge"])[0]
    assert marked != "Rebase (Recommended)"
    assert ct.strip_recommended(marked) == "Rebase"
