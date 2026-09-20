"""The whole service against a real embedding model (Ollama), a real Postgres and real documents,
with nothing faked: the gateway runs in this process, the documents are read by the isolated
reader, the vectors go through pgvector.

Opt-in, because it needs Docker's Postgres and a running Ollama with the model pulled:

    FACTOTUM_POSTGRES_PASSWORD=dev FACTOTUM_LIVE_TESTS=1 \\
        uv run pytest -m live services/knowledge/tests/live -s

``-s`` shows what was measured. A book to read (a PDF of your own; it is never copied anywhere)
is pointed to with FACTOTUM_LIVE_BOOK; without it the book test is skipped.

The model and the server can be changed with FACTOTUM_LIVE_OLLAMA_URL and
FACTOTUM_LIVE_OLLAMA_EMBED_MODEL: a multilingual model shows what the default cannot, that a
question in one language finds a passage in another.
"""

import asyncio
import os
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from factotum.common.config import Environment, ServiceSettings
from factotum.common.devauth import DEV_TENANT_ID, DEV_USER_ID, DevAuthenticator
from factotum.common.urlguard import UrlPolicy
from factotum.contracts.identity import Owner, Role
from factotum.contracts.knowledge import (
    Collection,
    CollectionCreate,
    Document,
    DocumentStatus,
    DocumentText,
    SearchRequest,
)
from factotum.contracts.models import BindingOptions, ModelRole, ProviderKind
from factotum.contracts.testing import make_model_binding, make_user_principal
from factotum.db.pool import Database
from factotum.db.settings import DatabaseSettings
from factotum.knowledge.ingestion import IngestionSettings, Ingestor
from factotum.knowledge.service import KnowledgeService
from factotum.knowledge.store import Hit
from factotum.knowledge.store.postgres import (
    PostgresStore,
    prepare,
    vector_index_name,
    vector_search_sql,
    vector_text,
)
from factotum.knowledge.worker import Worker
from factotum.model_client import DevDelegation, GatewayEmbedder
from factotum.model_gateway.api import create_app
from factotum.model_gateway.bindings import InMemoryBindingRepository
from factotum.model_gateway.core import ModelGateway, guarded_clients
from factotum.model_gateway.secrets import EnvSecretResolver

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("FACTOTUM_LIVE_TESTS") != "1",
        reason="live tests are opt-in: set FACTOTUM_LIVE_TESTS=1 (and run Ollama and Postgres)",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]

URL = os.environ.get("FACTOTUM_LIVE_OLLAMA_URL", "http://127.0.0.1:11434")
EMBED_MODEL = os.environ.get("FACTOTUM_LIVE_OLLAMA_EMBED_MODEL", "nomic-embed-text")
BOOK = os.environ.get("FACTOTUM_LIVE_BOOK")

PRINCIPAL = make_user_principal(
    tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID, roles=frozenset({Role.ADMIN})
)


@dataclass
class Stack:
    """Everything the service is made of, running."""

    database: Database
    store: PostgresStore
    service: KnowledgeService
    embedder: GatewayEmbedder
    owner: Owner

    async def collection(self, name: str) -> Collection:
        return await self.service.create_collection(PRINCIPAL, CollectionCreate(name=name))

    async def until_read(self, documents: list[Document], seconds: float = 1800) -> list[Document]:
        deadline = time.monotonic() + seconds
        while True:
            current = [await self.service.get_document(PRINCIPAL, d.id) for d in documents]
            if all(d.status in {DocumentStatus.READY, DocumentStatus.FAILED} for d in current):
                return current
            if time.monotonic() > deadline:
                raise AssertionError("the documents were not read in time")
            await asyncio.sleep(0.5)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def stack(database_settings: DatabaseSettings) -> AsyncIterator[Stack]:
    settings = ServiceSettings(environment=Environment.TEST, _env_file=None)
    gateway = ModelGateway(
        InMemoryBindingRepository(
            [
                make_model_binding(
                    tenant_id=DEV_TENANT_ID,
                    role=ModelRole.EMBEDDING,
                    provider=ProviderKind.OLLAMA,
                    model=EMBED_MODEL,
                    base_url=URL,
                    options=BindingOptions(timeout_s=300),
                )
            ]
        ),
        EnvSecretResolver(),
        # An on-premise deployment: the model is on this machine.
        client_factory=guarded_clients(UrlPolicy.private_networks_allowed()),
    )
    gateway_app = create_app(
        gateway, DevAuthenticator(settings, tenant_id=DEV_TENANT_ID, user_id=DEV_USER_ID)
    )
    database = await Database.connect(database_settings)
    await prepare(database)
    store = PostgresStore(database)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway_app),
        base_url="http://gateway",
        timeout=httpx.Timeout(300.0),
    ) as client:
        embedder = GatewayEmbedder(client, DevDelegation(settings), batch=32)
        ingestor = Ingestor(store, embedder, settings=IngestionSettings())
        stop = asyncio.Event()
        working = asyncio.create_task(
            Worker(store, ingestor, concurrency=2, poll_seconds=0.2).run(stop)
        )
        try:
            yield Stack(
                database=database,
                store=store,
                service=KnowledgeService(store, embedder),
                embedder=embedder,
                owner=Owner.of_subject(PRINCIPAL.acts_as),
            )
        finally:
            stop.set()
            await asyncio.wait_for(working, 60)
    await gateway.aclose()
    await database.close()


def rank_of(title: str, passages: list[str]) -> int | None:
    """Where the first passage of a document with this title is (1 is the top), if it is there."""
    for place, found in enumerate(passages, start=1):
        if found == title:
            return place
    return None


# ---- a small library in two languages ------------------------------------------------------------

LIBRARY: dict[str, str] = {
    "Router manual": (
        "# Router\n\n## Factory reset\n\nTo restore the factory settings of the router, hold the "
        "reset button for ten seconds until the light blinks. All custom settings, including the "
        "WiFi password, will be erased.\n\n## Firmware\n\nThe firmware is updated from the "
        "administration page. Do not switch the router off during the update."
    ),
    "Warranty": (
        "The warranty covers manufacturing defects for two years from the day of purchase. Damage "
        "caused by water, drops or unauthorized repairs is not covered. To claim the warranty keep "
        "the receipt."
    ),
    "Sourdough recipe": (
        "To make sourdough bread, mix flour, water and salt with an active starter. Let the dough "
        "rest overnight, then bake it in a hot oven with steam for forty minutes."
    ),
    "Backup guide": (
        "Backups should follow the 3-2-1 rule: three copies of your data, on two different media, "
        "with one copy stored off site. Test restoring from a backup at least once a year."
    ),
    "Manuale del router": (
        "# Router\n\n## Ripristino\n\nPer ripristinare le impostazioni di fabbrica del "
        "router, tieni premuto il pulsante di reset per dieci secondi finché la luce lampeggia. "
        "Tutte le impostazioni personalizzate, compresa la password del WiFi, verranno "
        "cancellate."
    ),
    "Garanzia": (
        "La garanzia copre i difetti di fabbricazione per due anni dalla data di acquisto. "
        "I danni causati da acqua, cadute o riparazioni non autorizzate non sono coperti. "
        "Per far valere la garanzia conserva lo scontrino."
    ),
    "Ricetta del pane": (
        "Per fare il pane con il lievito madre, mescola farina, acqua e sale con il lievito "
        "attivo. Lascia riposare l'impasto per tutta la notte, poi cuocilo in forno caldo con "
        "vapore per quaranta minuti."
    ),
    "Guida ai backup": (
        "I backup dovrebbero seguire la regola 3-2-1: tre copie dei dati, su due supporti diversi, "
        "con una copia conservata fuori sede. Prova a ripristinare da un backup almeno una volta "
        "all'anno."
    ),
}

QUESTIONS: list[tuple[str, str, str]] = [
    # (question, the document that answers it, which languages it crosses)
    ("How do I restore my router to factory settings?", "Router manual", "en>en"),
    ("What does the warranty not cover?", "Warranty", "en>en"),
    ("How long should I bake sourdough bread?", "Sourdough recipe", "en>en"),
    ("How often should I test my backups?", "Backup guide", "en>en"),
    (
        "Come faccio a ripristinare il router alle impostazioni di fabbrica?",
        "Manuale del router",
        "it>it",
    ),
    ("Cosa non copre la garanzia?", "Garanzia", "it>it"),
    ("Per quanto tempo devo cuocere il pane?", "Ricetta del pane", "it>it"),
    ("Ogni quanto devo provare i miei backup?", "Guida ai backup", "it>it"),
    (
        "Come faccio a ripristinare il router alle impostazioni di fabbrica?",
        "Router manual",
        "it>en",
    ),
    ("Cosa non copre la garanzia?", "Warranty", "it>en"),
    ("Per quanto tempo devo cuocere il pane?", "Sourdough recipe", "it>en"),
    ("Ogni quanto devo provare i miei backup?", "Backup guide", "it>en"),
    ("How do I restore my router to factory settings?", "Manuale del router", "en>it"),
    ("What does the warranty not cover?", "Garanzia", "en>it"),
    ("How long should I bake sourdough bread?", "Ricetta del pane", "en>it"),
    ("How often should I test my backups?", "Guida ai backup", "en>it"),
]


async def test_a_small_library_in_two_languages_is_found_by_meaning_and_by_words(
    stack: Stack,
) -> None:
    collection = await stack.collection("Library")
    added = [
        await stack.service.add_text(PRINCIPAL, collection.id, DocumentText(title=title, text=text))
        for title, text in LIBRARY.items()
    ]
    read = await stack.until_read(added)
    assert [d.status for d in read] == [DocumentStatus.READY] * len(LIBRARY), [
        (d.title, d.error_message) for d in read
    ]
    languages = {d.title: d.language for d in read}
    print(f"\n  model {EMBED_MODEL}; languages detected: {languages}")

    by_pair: dict[str, list[tuple[int | None, int | None, int | None]]] = {}
    for question, answer, pair in QUESTIONS:
        meaning = await stack.embedder.embed(stack.owner, [question])
        by_meaning = await stack.store.search_vector(
            DEV_TENANT_ID, collection.id, meaning.vectors[0], limit=8
        )
        by_words = await stack.store.search_text(DEV_TENANT_ID, [collection.id], question, limit=8)
        both = await stack.service.search(
            PRINCIPAL, SearchRequest(query=question, collection_ids=(collection.id,), limit=8)
        )
        by_pair.setdefault(pair, []).append(
            (
                rank_of(answer, [h.document_title for h in by_meaning]),
                rank_of(answer, [h.document_title for h in by_words]),
                rank_of(answer, [p.document_title for p in both.passages]),
            )
        )
        assert both.warnings == ()

    def show(rank: int | None) -> str:
        return "-" if rank is None else str(rank)

    print("\n  rank of the right document (1 is the top, - is not found)")
    print("  pair    meaning  words  both")
    for pair, results in by_pair.items():
        asked = [q for q in QUESTIONS if q[2] == pair]
        for (by_meaning_at, by_words_at, by_both_at), (_, answer, kind) in zip(
            results, asked, strict=True
        ):
            print(
                f"  {kind}    {show(by_meaning_at):>5}  {show(by_words_at):>5}  "
                f"{show(by_both_at):>4}   {answer}"
            )

    # What must hold with any embedding model: in one language, the right document is at the top.
    for pair in ("en>en", "it>it"):
        for _, _, at in by_pair[pair]:
            assert at is not None
            assert at <= 2, f"{pair}: the right document was at {at}"


# ---- a whole book -----------------------------------------------------------------------------

Expect = list[tuple[str, ...]]
"""Alternatives; each is the words a passage must all have to be one that answers."""

# What a person would ask of a book about Linux, without naming the command that answers: what is
# measured is finding it by what it does. The same, in Italian, for a book that is in English.
ASKED_IN_ENGLISH: list[tuple[str, Expect]] = [
    ("How do I change the permissions of a file?", [("chmod",)]),
    ("How do I change the owner of a file?", [("chown",)]),
    ("How do I copy files from one directory to another?", [("cp",)]),
    ("How do I rename or move a file?", [("mv",)]),
    ("How do I delete a directory and everything in it?", [("rm",)]),
    ("How do I find a file by its name?", [("find", "-name"), ("locate",)]),
    ("How do I check how much disk space is free?", [("df",)]),
    ("How do I see how much space a directory uses?", [("du",)]),
    ("How do I display the contents of a text file in the terminal?", [("cat",)]),
    ("How do I change my password?", [("passwd",)]),
    ("How do I show the IP address of my network interface?", [("ip", "addr"), ("ifconfig",)]),
    ("How do I test whether another computer is reachable on the network?", [("ping",)]),
    ("How do I download a file from the web at the command line?", [("wget",), ("curl",)]),
    ("How do I read the system logs?", [("journalctl",), ("/var/log/messages",)]),
    ("How do I see which ports are listening?", [("ss",), ("netstat",)]),
    ("How do I create a new partition on a disk?", [("fdisk",), ("parted",)]),
    ("How do I put a filesystem on a partition?", [("mkfs",)]),
    ("How do I compress a file?", [("gzip",), ("bzip2",), ("xz",)]),
    ("How do I compare two files line by line?", [("diff",)]),
    ("How do I count the lines in a file?", [("wc",)]),
    ("How do I replace text in a file from the command line?", [("sed",)]),
    ("How do I set an environment variable?", [("export",)]),
    ("How do I create a shortcut for a long command?", [("alias",)]),
    ("How do I run a program in the background?", [("bg",), ("jobs",)]),
    ("How do I change the priority of a running process?", [("nice",), ("renice",)]),
    ("How do I add a user to a group?", [("usermod",), ("groupadd",), ("gpasswd",)]),
    ("How do I safely edit the sudo configuration?", [("visudo",)]),
    ("How do I copy files to another machine over ssh?", [("scp",), ("rsync",)]),
    ("How do I manage Kubernetes pods from the command line?", [("kubectl",)]),
    ("How do I change the hostname of the machine?", [("hostnamectl",), ("hostname",)]),
    ("How do I show the manual page of a command?", [("man",)]),
    ("How can I schedule a task to run automatically at a set time?", [("cron",), ("crontab",)]),
    ("How do I add a new user to the system?", [("useradd",)]),
    ("How do I attach a disk partition to the filesystem tree?", [("mount",)]),
    ("How do I install software from the command line?", [("dnf",), ("yum",), ("apt",)]),
    ("How do I create a key pair for passwordless remote login?", [("ssh-keygen",)]),
    ("How do I start, stop and enable a service?", [("systemctl",)]),
    ("How do I search for a word inside many files?", [("grep",)]),
    ("How do I bundle and compress a directory into an archive?", [("tar",)]),
    ("How do I open a port in the firewall?", [("firewall-cmd",), ("firewalld",), ("iptables",)]),
    ("How do I create a logical volume that spans several disks?", [("lvcreate",), ("lvm",)]),
    ("How do I run a command with administrator privileges?", [("sudo",)]),
    ("How do I terminate a process that does not respond?", [("kill",)]),
    ("How do I run an application in a container?", [("podman",), ("docker",)]),
]

ASKED_IN_ITALIAN: list[tuple[str, Expect]] = [
    ("Come cambio i permessi di un file?", [("chmod",)]),
    ("Come cambio il proprietario di un file?", [("chown",)]),
    ("Come copio dei file da una cartella a un'altra?", [("cp",)]),
    ("Come rinomino o sposto un file?", [("mv",)]),
    ("Come elimino una cartella con tutto il suo contenuto?", [("rm",)]),
    ("Come trovo un file dal suo nome?", [("find", "-name"), ("locate",)]),
    ("Come controllo quanto spazio libero c'è sul disco?", [("df",)]),
    ("Come vedo quanto spazio occupa una cartella?", [("du",)]),
    ("Come mostro il contenuto di un file di testo nel terminale?", [("cat",)]),
    ("Come cambio la mia password?", [("passwd",)]),
    ("Come verifico se un altro computer è raggiungibile in rete?", [("ping",)]),
    ("Come scarico un file da internet dalla riga di comando?", [("wget",), ("curl",)]),
    ("Come leggo i log di sistema?", [("journalctl",), ("/var/log/messages",)]),
    ("Come creo una nuova partizione su un disco?", [("fdisk",), ("parted",)]),
    ("Come comprimo un file?", [("gzip",), ("bzip2",), ("xz",)]),
    ("Come confronto due file riga per riga?", [("diff",)]),
    ("Come conto le righe di un file?", [("wc",)]),
    ("Come sostituisco del testo in un file dalla riga di comando?", [("sed",)]),
    ("Come imposto una variabile d'ambiente?", [("export",)]),
    ("Come cambio la priorità di un processo in esecuzione?", [("nice",), ("renice",)]),
    ("Come aggiungo un utente a un gruppo?", [("usermod",), ("groupadd",), ("gpasswd",)]),
    ("Come copio dei file su un'altra macchina via ssh?", [("scp",), ("rsync",)]),
    (
        "Come pianifico un'attività da eseguire automaticamente a un'ora stabilita?",
        [("cron",), ("crontab",)],
    ),
    ("Come aggiungo un nuovo utente al sistema?", [("useradd",)]),
    ("Come collego una partizione del disco all'albero del filesystem?", [("mount",)]),
    ("Come installo un programma dalla riga di comando?", [("dnf",), ("yum",), ("apt",)]),
    ("Come creo una coppia di chiavi per accedere da remoto senza password?", [("ssh-keygen",)]),
    ("Come avvio, fermo e abilito un servizio?", [("systemctl",)]),
    ("Come cerco una parola dentro molti file?", [("grep",)]),
    ("Come raggruppo e comprimo una cartella in un archivio?", [("tar",)]),
    ("Come apro una porta nel firewall?", [("firewall-cmd",), ("firewalld",), ("iptables",)]),
    ("Come eseguo un comando con i privilegi di amministratore?", [("sudo",)]),
    ("Come termino un processo che non risponde?", [("kill",)]),
    ("Come faccio girare un'applicazione in un container?", [("podman",), ("docker",)]),
]

IDENTIFIERS = [
    "ssh-keygen",
    "getenforce",
    "lvcreate",
    "firewall-cmd",
    "systemctl",
    "restorecon",
    "hostnamectl",
    "journalctl",
    "visudo",
    "usermod",
    "chcon",
    "mkfs",
    "exportfs",
    "podman",
    "kubectl",
    "useradd",
    "crontab",
    "nmcli",
    "setenforce",
    "ausearch",
    "rsync",
    "dnf",
]
"""A person asks for a name as it is written: what a vector blurs and the words find."""


def answers(text: str, expect: Expect) -> bool:
    lowered = text.lower()
    return any(
        all(re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", lowered) for word in alternative)
        for alternative in expect
    )


def one_per_section(hits: list[Hit]) -> list[Hit]:
    """The best piece of each section, as a person is given them."""
    seen: set[object] = set()
    kept: list[Hit] = []
    for hit in hits:
        if hit.parent_id not in seen:
            seen.add(hit.parent_id)
            kept.append(hit)
    return kept


def score(hits: list[Hit], expect: Expect, k: int = 5) -> tuple[bool, float]:
    """Whether one of the first ``k`` sections answers, and what share of them do. A section
    answers when it is what a person is shown of it: the whole section."""
    right = [answers(hit.context, expect) for hit in one_per_section(hits)[:k]]
    return any(right), sum(right) / k


@pytest.mark.skipif(
    not BOOK or not Path(BOOK or "").is_file(), reason="FACTOTUM_LIVE_BOOK is not set"
)
async def test_a_whole_book_is_read_and_answers_questions_in_two_languages(stack: Stack) -> None:
    assert BOOK is not None
    book = Path(BOOK)
    collection = await stack.collection("The book")

    started = time.monotonic()
    content = await asyncio.to_thread(book.read_bytes)
    document = await stack.service.add_file(
        PRINCIPAL, collection.id, content=content, filename=book.name
    )
    [read] = await stack.until_read([document])
    elapsed = time.monotonic() - started
    assert read.status is DocumentStatus.READY, read.error_message

    async with stack.database.connection() as connection:
        parents = await connection.fetchval("SELECT count(*) FROM knowledge.parents")
        # The document is searchable as soon as it is ready; the index is built just after it.
        indexed: bool | None = None
        for _ in range(240):
            indexed = await connection.fetchval(
                "SELECT indisvalid FROM pg_index WHERE indexrelid = $1::text::regclass",
                f"knowledge.{vector_index_name(collection.id)}",
            )
            if indexed:
                break
            await asyncio.sleep(0.5)
    dimensions = (await stack.service.get_collection(PRINCIPAL, collection.id)).dimensions
    print(
        f"\n  {book.name}: {read.chunk_count} pieces in {parents} sections, "
        f"{dimensions} dimensions, language {read.language}, read in {elapsed:.0f} s, "
        f"index {'valid' if indexed else 'missing'}"
    )
    assert read.chunks_embedded == read.chunk_count > 1000
    assert indexed is True

    # The search by meaning is answered by the index, on real vectors.
    probe = await stack.embedder.embed(stack.owner, ["how do I change the permissions of a file"])

    async def planned(*, asked_for_the_index: bool) -> str:
        async with stack.database.transaction() as connection:
            if asked_for_the_index:
                await connection.execute("SET LOCAL enable_seqscan = off")
                await connection.execute("SET LOCAL enable_sort = off")
            plan = await connection.fetchval(
                "EXPLAIN (FORMAT JSON) " + vector_search_sql(collection.id, dimensions),
                DEV_TENANT_ID,
                vector_text(probe.vectors[0]),
                20,
            )
        return str(plan)

    # The query is written in a way the index can answer. Whether the planner, left to itself, uses
    # it is its call, by cost: for a collection of this size reading every piece may be cheaper.
    assert vector_index_name(collection.id) in await planned(asked_for_the_index=True)
    left_to_itself = vector_index_name(collection.id) in await planned(asked_for_the_index=False)
    print(
        "  left to itself the planner "
        + ("uses the index" if left_to_itself else "reads every piece (cheaper at this size)")
    )

    async def by_meaning(question: str) -> list[Hit]:
        vector = (await stack.embedder.embed(stack.owner, [question])).vectors[0]
        return await stack.store.search_vector(DEV_TENANT_ID, collection.id, vector, limit=40)

    async def by_words(question: str) -> list[Hit]:
        return await stack.store.search_text(DEV_TENANT_ID, [collection.id], question, limit=40)

    latencies: list[float] = []

    async def by_both(question: str) -> list[Hit]:
        began = time.perf_counter()
        # Only the book, like the other two: another collection in the database (the small library
        # of the other test) would put its pieces among the book's and change what is compared.
        found = await stack.service.search(
            PRINCIPAL, SearchRequest(query=question, collection_ids=(collection.id,), limit=5)
        )
        latencies.append(time.perf_counter() - began)
        return [
            Hit(  # the passages, shown as hits to be scored in the same way as the others
                chunk_id=p.chunk_id,
                collection_id=p.collection_id,
                document_id=p.document_id,
                parent_id=p.chunk_id,
                document_title=p.document_title,
                source=p.source,
                text=p.text,
                context=p.context,
                headings=p.location.headings,
                page=p.location.page,
                language=p.language,
                score=p.score,
            )
            for p in found.passages
        ]

    print("\n  hit@5 (a section that answers among the first five) / precision@5")
    print(f"  {'asked':<24} {'meaning':>13} {'words':>13} {'both':>13}")
    results: dict[str, dict[str, float]] = {}
    asked: list[tuple[str, list[tuple[str, Expect]]]] = [
        ("in English (44)", ASKED_IN_ENGLISH),
        ("in Italian (34)", ASKED_IN_ITALIAN),
        ("by name (22)", [(name, [(name,)]) for name in IDENTIFIERS]),
    ]
    for label, questions in asked:
        cells: list[str] = []
        for method, find in (("meaning", by_meaning), ("words", by_words), ("both", by_both)):
            found = share = 0.0
            for question, expect in questions:
                hit, precision = score(await find(question), expect)
                found += hit
                share += precision
            found, share = found / len(questions), share / len(questions)
            results.setdefault(label, {})[method] = found
            cells.append(f"{found:5.0%} / {share:4.0%}")
        print(f"  {label:<24} {cells[0]:>13} {cells[1]:>13} {cells[2]:>13}")

    ordered = sorted(latencies)
    print(
        f"\n  a search (embedding the question included): {len(ordered)} of them, "
        f"median {ordered[len(ordered) // 2] * 1000:.0f} ms, "
        f"90th percentile {ordered[int(len(ordered) * 0.9)] * 1000:.0f} ms, "
        f"slowest {ordered[-1] * 1000:.0f} ms"
    )

    # What must hold with any embedding model: in English the book answers most of what is asked,
    # and asking for a name as written finds it. The search by both is not worse than by words.
    assert results["in English (44)"]["both"] >= 0.75
    assert results["by name (22)"]["both"] >= 0.75
    assert results["in English (44)"]["both"] >= results["in English (44)"]["words"]
