"""A document that is a file: a PDF and a Word file, uploaded as a person uploads them, read by the
knowledge service in a process of its own (the one thing that reads what somebody else wrote, and
has nothing but the bytes and a limit of time and memory), and asked about by voice.

The text documents of the other modules are read where the test is; these are the ones a person
really has, with the loaders and the isolation of the service."""

from factotum.contracts.edge import TurnStatus
from factotum.contracts.knowledge import CollectionOwner, DocumentStatus
from factotum.e2e.devices import connected
from factotum.e2e.documents import PDF, WORD, a_pdf, a_word_document
from factotum.e2e.house import running
from factotum.e2e.models import FROM_YOUR_DOCUMENTS_IT, NOTHING_TO_GO_ON, ROUTER, SPOKEN_ANSWER

QUESTION = "How do I reset the router?"
MANUAL = [
    ("Introduction", ["This manual describes the router in the study."]),
    ("Resetting the router", [ROUTER]),
]
ITALIAN_MANUAL = [
    ("Il router", ["Il router è nello studio, accanto alla libreria, perché lì c'è il cavo."])
]


class TestAFileThatWasUploaded:
    async def test_a_pdf_is_answered_from_with_the_page_it_comes_from(self) -> None:
        async with running() as house:
            document = await house.library.upload(
                "Home",
                "router.pdf",
                a_pdf(MANUAL),
                PDF,
                owner=CollectionOwner.TENANT,
                title="Router manual",
            )
            async with connected(house, "smart-display") as display:
                ended = await display.says_aloud(QUESTION)

                assert document.status is DocumentStatus.READY, document
                assert ended.status is TurnStatus.FINISHED, ended
                assert display.spoken() == SPOKEN_ANSWER
                [card] = display.shown.of("card")
                assert [(item.label, item.value) for item in card.items] == [
                    ("[1] Router manual", "page 2 · Resetting the router")
                ]

    async def test_a_word_file_is_answered_from_with_the_heading_it_comes_from(self) -> None:
        async with running() as house:
            document = await house.library.upload(
                "Home",
                "router.docx",
                a_word_document(MANUAL),
                WORD,
                owner=CollectionOwner.TENANT,
                title="Router manual",
            )
            async with connected(house, "smart-display") as display:
                ended = await display.says_aloud(QUESTION)

                assert document.status is DocumentStatus.READY, document
                assert ended.status is TurnStatus.FINISHED, ended
                assert display.spoken() == SPOKEN_ANSWER
                [card] = display.shown.of("card")
                assert [(item.label, item.value) for item in card.items] == [
                    ("[1] Router manual", "Resetting the router")
                ]

    async def test_a_file_in_italian_keeps_its_accents_from_the_page_to_the_speaker(self) -> None:
        async with running() as house:
            await house.library.upload(
                "Casa",
                "router.pdf",
                a_pdf(ITALIAN_MANUAL),
                PDF,
                owner=CollectionOwner.TENANT,
                title="Il router",
            )
            async with connected(house, "speaker") as speaker:
                await speaker.says_aloud("Dov'è il router?")

                assert speaker.spoken()[0] == (
                    f"{FROM_YOUR_DOCUMENTS_IT} Il router è nello studio, accanto alla libreria, "
                    "perché lì c'è il cavo."
                )

    async def test_a_pdf_that_is_broken_fails_where_it_was_read_and_nothing_of_it_is_kept(
        self,
    ) -> None:
        async with running() as house:
            document = await house.library.upload(
                "Home",
                "router.pdf",
                b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R",
                PDF,
                owner=CollectionOwner.TENANT,
            )
            async with connected(house, "speaker") as speaker:
                await speaker.says_aloud(QUESTION)

                # The process that read it came to nothing and the service did not: the document is
                # one that failed, and the question was answered as one that has no document: with
                # nothing to answer from, the code says so and no model is asked.
                assert document.status is DocumentStatus.FAILED, document
                assert document.error_code is not None
                assert house.model.prompts == []
                assert speaker.spoken() == [NOTHING_TO_GO_ON]

    async def test_a_file_is_read_for_what_its_bytes_are_and_not_for_what_it_is_said_to_be(
        self,
    ) -> None:
        async with running() as house:
            document = await house.library.upload(
                "Home",
                "router.pdf",
                ROUTER.encode(),
                PDF,
                owner=CollectionOwner.TENANT,
                title="Router manual",
            )
            async with connected(house, "speaker") as speaker:
                await speaker.says_aloud(QUESTION)

                assert (document.status, document.media_type) == (
                    DocumentStatus.READY,
                    "text/plain",
                )
                assert speaker.spoken() == SPOKEN_ANSWER
