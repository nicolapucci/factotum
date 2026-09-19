# Factotum

A self-hosted voice assistant that answers from **your own documents** and from the **web**, using **the models you choose**.

> Status: design phase. No services have been implemented yet.

## Principles

- **You choose the models.** Every model (chat, embeddings, speech-to-text, text-to-speech) and the web search backend is provided by the user: a local server, or a hosted provider with the user's own key. No service in the system knows or hardcodes which model it is talking to.
- **You control your documents.** Each collection carries a data policy that decides whether its content may leave the machine. The system enforces it; it is not left to convention.
- **Tested by default.** Every piece of code ships with unit tests.

## License

Not chosen yet.
