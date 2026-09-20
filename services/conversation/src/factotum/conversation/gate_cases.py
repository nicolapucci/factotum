"""Messages labelled with how the reference gate should read each of them, to measure a model with.

The gate is asked in the ``chat.fast`` role, and the user chooses the model bound to it (ADR 0008),
so what a model does with this question is worth knowing before it is bound: the live tests of the
conversation service run these messages through it (see ``docs/conversation.md``).

None of them is among the examples the gate shows a model (``EXAMPLES`` in ``gate``), and a test
says so: a model given the answer to a question is not being measured. They are in English and in
Italian, and they are meant to be the ordinary things said to an assistant, not tricks.

The first messages of each kind were written first and looked at while the prompt was being changed;
the others were written afterwards and used to choose between the changes. Neither set was ever an
example. Both are small, and what is measured with them is a guide, not a promise.
"""

from dataclasses import dataclass

from factotum.contracts.conversation import Period, ReferenceKind

NONE = ReferenceKind.NONE
CONTINUATION = ReferenceKind.CONTINUATION
EARLIER = ReferenceKind.EARLIER


@dataclass(frozen=True, slots=True)
class GateCase:
    message: str
    kind: ReferenceKind
    when: Period | None = None
    """For an earlier exchange: the period the message names, if it names one."""

    topic: tuple[str, ...] = ()
    """For an earlier exchange: words of which at least one is expected in the topic."""


GATE_CASES: tuple[GateCase, ...] = (
    # --- it can be understood on its own: most of what is said --------------------------------
    GateCase("What is the capital of Australia?", NONE),
    GateCase("How do I restore my router to factory settings?", NONE),
    GateCase("What does the warranty not cover?", NONE),
    GateCase("Come si resetta il router?", NONE),
    GateCase("Qual è la capitale dell'Australia?", NONE),
    GateCase("Turn on the kitchen lights", NONE),
    GateCase("Accendi le luci della cucina", NONE),
    GateCase("How long should I bake sourdough bread?", NONE),
    GateCase("Quanto tempo deve cuocere il pane?", NONE),
    GateCase("Tell me a joke about cats", NONE),
    GateCase("What is 15 percent of 240?", NONE),
    GateCase("Che tempo fa oggi a Roma?", NONE),
    GateCase("What did the Romans eat for breakfast?", NONE),
    GateCase("Chi ha vinto i mondiali di calcio del 2006?", NONE),
    GateCase("Remind me to call the dentist tomorrow", NONE),
    GateCase("How often should I test my backups?", NONE),
    GateCase("Cosa dice il manuale sull'aggiornamento del firmware?", NONE),
    GateCase("Explain the 3-2-1 backup rule", NONE),
    GateCase("Good morning!", NONE),
    GateCase("Buongiorno, come stai?", NONE),
    GateCase("How many ounces are in a pound?", NONE),
    GateCase("Write a haiku about autumn", NONE),
    GateCase("What is the difference between TCP and UDP?", NONE),
    GateCase("Set a timer for ten minutes", NONE),
    GateCase("Summarize the warranty terms", NONE),
    GateCase("Who painted the Mona Lisa?", NONE),
    GateCase("Translate 'good night' into Spanish", NONE),
    GateCase("Is it going to rain in London tomorrow?", NONE),
    GateCase("How do I change the WiFi password on the router?", NONE),
    GateCase("List three ways to save energy at home", NONE),
    GateCase("What time is it in Tokyo?", NONE),
    GateCase("Thanks!", NONE),
    GateCase("Quanti grammi ci sono in un etto?", NONE),
    GateCase("Scrivimi una poesia sul mare", NONE),
    GateCase("Qual è la differenza tra un virus e un batterio?", NONE),
    GateCase("Imposta un timer di dieci minuti", NONE),
    GateCase("Spegni la luce del salotto", NONE),
    GateCase("Chi ha dipinto la Gioconda?", NONE),
    GateCase("Traduci 'buonanotte' in inglese", NONE),
    GateCase("Domani pioverà a Milano?", NONE),
    GateCase("Come si cambia la password del WiFi?", NONE),
    GateCase("Che ore sono a New York?", NONE),
    GateCase("Grazie mille", NONE),
    GateCase("Cosa copre la garanzia?", NONE),
    # --- it carries on the exchange just before ------------------------------------------------
    GateCase("And in English?", CONTINUATION),
    GateCase("E in italiano?", CONTINUATION),
    GateCase("Why?", CONTINUATION),
    GateCase("Perché?", CONTINUATION),
    GateCase("The second one", CONTINUATION),
    GateCase("E il secondo?", CONTINUATION),
    GateCase("Repeat that, please", CONTINUATION),
    GateCase("Ripeti per favore", CONTINUATION),
    GateCase("And how long does it take?", CONTINUATION),
    GateCase("E quanto ci vuole?", CONTINUATION),
    GateCase("What about the blue one?", CONTINUATION),
    GateCase("Can you make it shorter?", CONTINUATION),
    GateCase("Please continue", CONTINUATION),
    GateCase("Are you sure?", CONTINUATION),
    GateCase("Give me another example", CONTINUATION),
    GateCase("How much does it cost?", CONTINUATION),
    GateCase("Explain that in simpler words", CONTINUATION),
    GateCase("And the day after?", CONTINUATION),
    GateCase("E quello blu?", CONTINUATION),
    GateCase("Puoi farlo più corto?", CONTINUATION),
    GateCase("Continua pure", CONTINUATION),
    GateCase("Sei sicuro?", CONTINUATION),
    GateCase("Fammi un altro esempio", CONTINUATION),
    GateCase("E quanto costa?", CONTINUATION),
    GateCase("Spiegamelo con parole più semplici", CONTINUATION),
    GateCase("E il giorno dopo?", CONTINUATION),
    # --- it points to something said earlier ---------------------------------------------------
    GateCase(
        "What did you tell me yesterday about the router?", EARLIER, Period.YESTERDAY, ("router",)
    ),
    GateCase("Cosa mi hai detto ieri sul router?", EARLIER, Period.YESTERDAY, ("router",)),
    GateCase("The recipe you gave me last week", EARLIER, Period.LAST_WEEK, ("recipe",)),
    GateCase(
        "La ricetta che mi hai dato la settimana scorsa", EARLIER, Period.LAST_WEEK, ("ricetta",)
    ),
    GateCase("What did I ask you this morning?", EARLIER, Period.TODAY),
    GateCase("Cosa ti ho chiesto stamattina?", EARLIER, Period.TODAY),
    GateCase(
        "Remember the backup schedule we talked about?", EARLIER, None, ("backup", "schedule")
    ),
    GateCase("Ti ricordi quel libro che mi avevi consigliato?", EARLIER, None, ("libro", "book")),
    GateCase(
        "What was that thing you told me about the warranty last month?",
        EARLIER,
        Period.LAST_MONTH,
        ("warranty",),
    ),
    GateCase("Quella cosa che mi dicesti sul firmware", EARLIER, None, ("firmware",)),
    GateCase(
        "What was the name of the restaurant you suggested last month?",
        EARLIER,
        Period.LAST_MONTH,
        ("restaurant",),
    ),
    GateCase(
        "Go back to what we discussed yesterday about taxes", EARLIER, Period.YESTERDAY, ("tax",)
    ),
    GateCase("You mentioned a podcast earlier this week", EARLIER, Period.THIS_WEEK, ("podcast",)),
    GateCase("What did we say about the vacation plan?", EARLIER, None, ("vacation",)),
    GateCase("Earlier today you gave me a workout routine", EARLIER, Period.TODAY, ("workout",)),
    GateCase(
        "Quel ristorante che mi hai suggerito il mese scorso",
        EARLIER,
        Period.LAST_MONTH,
        ("ristorante",),
    ),
    GateCase(
        "Torniamo a quello che ci siamo detti ieri sulle tasse",
        EARLIER,
        Period.YESTERDAY,
        ("tasse",),
    ),
    GateCase(
        "Mi avevi parlato di un podcast questa settimana", EARLIER, Period.THIS_WEEK, ("podcast",)
    ),
    GateCase("Cosa avevamo deciso per il piano vacanze?", EARLIER, None, ("vacanz",)),
    GateCase("Oggi mi hai dato una scheda di allenamento", EARLIER, Period.TODAY, ("allenamento",)),
    GateCase(
        "Quella storia sui gatti che mi hai raccontato ieri sera",
        EARLIER,
        Period.YESTERDAY,
        ("gatti",),
    ),
    GateCase("The password tip you gave me the other day", EARLIER, None, ("password",)),
)
