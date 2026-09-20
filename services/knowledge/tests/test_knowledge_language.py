import pytest
from hypothesis import given
from hypothesis import strategies as st
from langdetect.lang_detect_exception import LangDetectException
from langdetect.language import Language

from factotum.knowledge import language
from factotum.knowledge.document import Block
from factotum.knowledge.language import detect_language, fts_config, normalize_language

ENGLISH = (
    "The router keeps its configuration in a file, and the file is read again every time the "
    "device starts. If the light does not stop blinking after a minute, unplug it and wait."
)
ITALIAN = (
    "Il router conserva la propria configurazione in un file, e il file viene letto di nuovo "
    "ogni volta che il dispositivo si avvia. Se la luce non smette di lampeggiare, scollegalo."
)
GERMAN = (
    "Der Router speichert seine Konfiguration in einer Datei, und die Datei wird bei jedem Start "
    "des Geräts neu gelesen. Wenn die Lampe nach einer Minute nicht aufhört zu blinken, trenne ihn."
)


def prose(*paragraphs: str) -> list[Block]:
    return [Block("text", paragraph) for paragraph in paragraphs]


class TestDetectLanguage:
    def test_english_prose_is_english(self) -> None:
        assert detect_language(prose(*[ENGLISH] * 6)) == "en"

    def test_italian_prose_is_italian(self) -> None:
        assert detect_language(prose(*[ITALIAN] * 6)) == "it"

    def test_german_prose_is_german(self) -> None:
        assert detect_language(prose(*[GERMAN] * 6)) == "de"

    def test_one_paragraph_is_enough(self) -> None:
        assert detect_language(prose(ITALIAN)) == "it"

    def test_a_document_is_in_the_language_of_most_of_its_prose(self) -> None:
        blocks = prose(*[ENGLISH] * 18, *[ITALIAN] * 3)

        assert detect_language(blocks) == "en"

    def test_code_and_tables_are_not_prose(self) -> None:
        listing = [Block("code", "def " + "function_name(argument) " * 20)] * 8
        table = [Block("table", "Model | Button\nA1 | left " * 20)] * 8

        assert detect_language([*listing, *table, *prose(ITALIAN)]) == "it"

    def test_headings_and_paragraphs_are_read_but_short_ones_are_not_enough(self) -> None:
        assert detect_language([Block("heading", "Il router"), Block("text", "Riavvio")]) is None

    def test_text_that_is_mostly_symbols_is_not_prose(self) -> None:
        noise = "0x7f 0x00 0xff " + "1234567890 " * 30

        assert detect_language(prose(noise)) is None

    def test_no_blocks_no_language(self) -> None:
        assert detect_language([]) is None

    def test_the_same_text_always_gives_the_same_answer(self) -> None:
        blocks = prose(ENGLISH, ITALIAN, GERMAN, ENGLISH)

        assert len({detect_language(blocks) for _ in range(5)}) == 1

    def test_a_language_that_is_not_clear_is_not_guessed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        split = [Language("en", 0.5), Language("it", 0.5)]
        monkeypatch.setattr(language, "detect_langs", lambda _: split)

        assert detect_language(prose(ENGLISH)) is None

    def test_a_window_the_detector_cannot_read_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = 0

        def detect(_: str) -> list[Language]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise LangDetectException(0, "no features")
            return [Language("en", 0.99)]

        monkeypatch.setattr(language, "detect_langs", detect)

        assert detect_language(prose(*[ENGLISH] * 6 * 2)) == "en"

    def test_when_the_detector_can_read_nothing_there_is_no_language(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def detect(_: str) -> list[Language]:
            raise LangDetectException(0, "no features")

        monkeypatch.setattr(language, "detect_langs", detect)

        assert detect_language(prose(ENGLISH)) is None

    def test_a_code_the_service_cannot_use_is_left_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(language, "detect_langs", lambda _: [Language("zh-cn", 0.9)])

        assert detect_language(prose(ENGLISH)) == "zh"

    @given(
        st.lists(
            st.builds(
                Block,
                kind=st.sampled_from(["heading", "text", "code", "table"]),
                text=st.text(min_size=1, max_size=400),
            ),
            max_size=12,
        )
    )
    def test_it_never_fails_whatever_the_text(self, blocks: list[Block]) -> None:
        result = detect_language(blocks)

        assert result is None or (2 <= len(result) <= 3 and result.islower())


class TestFtsConfig:
    @pytest.mark.parametrize(
        ("code", "config"),
        [
            ("en", "english"),
            ("it", "italian"),
            ("de", "german"),
            ("fr", "french"),
            ("es", "spanish"),
        ],
    )
    def test_a_language_postgres_knows_gets_its_own_configuration(
        self, code: str, config: str
    ) -> None:
        assert fts_config(code) == config

    @pytest.mark.parametrize("code", ["zh", "ja", "xx", "", None])
    def test_any_other_language_is_searched_without_stemming(self, code: str | None) -> None:
        assert fts_config(code) == "simple"

    def test_every_configuration_named_is_an_actual_postgres_one(self) -> None:
        # They are interpolated into SQL as identifiers, so they must stay plain words.
        assert all(
            config.isalpha() and config.islower() for config in language.FTS_CONFIGS.values()
        )


class TestNormalizeLanguage:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("en", "en"),
            ("EN", "en"),
            ("en-US", "en"),
            ("pt_BR", "pt"),
            ("  it  ", "it"),
            ("zh-cn", "zh"),
            ("fil", "fil"),
        ],
    )
    def test_it_keeps_the_language_and_drops_the_region(self, code: str, expected: str) -> None:
        assert normalize_language(code) == expected

    @pytest.mark.parametrize(
        "code", ["", " ", "english", "x1", "1", "en us", "--", "'; drop table"]
    )
    def test_anything_that_is_not_a_language_code_is_refused(self, code: str) -> None:
        assert normalize_language(code) is None
