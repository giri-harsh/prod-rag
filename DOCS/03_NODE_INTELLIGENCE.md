# 🧠 Node Intelligence: The Agentic Brain

The pipeline is a **LangGraph state machine with a real cycle**: `retrieve -> grade -> rewrite_query -> retrieve` (at most `MAX_RETRIEVAL_ITERATIONS`, default 2).
Grading uses the top FlashRank score (no extra LLM call); only `rewrite_query` calls an LLM (the cheap model). All nodes are `async`; tenant identity comes from the
server-set `config["configurable"]["tenant_id"]`.

```mermaid
graph TD
  gate_and_plan -->|blocked| END
  gate_and_plan -->|conversational| respond
  gate_and_plan -->|standalone search| cache_lookup
  gate_and_plan -->|follow-up| retrieve
  cache_lookup -->|hit| END
  cache_lookup -->|miss| retrieve
  retrieve -->|no hits| END
  retrieve --> grade
  grade -->|good| respond
  grade -->|poor, iterations left| rewrite_query
  rewrite_query --> retrieve
  grade -->|poor, exhausted| give_up
  give_up --> END
  respond --> output_guard --> END
```
The same cycle is returned (as Mermaid text) by `GET /graph` for admin keys.

---

## 🤖 The Graph Nodes

### 1. 🧭 The Planner Node
*   **Model**: Groq (Llama 3.3 70B)
*   **Logic**: The Planner is the entry point. It analyzes the entire conversation history and the new user message.
*   **Decisions**:
    *   `CONVERSATIONAL`: If the user says "Hi" or asks about something already in the chat memory, it skips the expensive search process.
    *   `TECHNICAL`: If the user asks a question about Kubernetes, Intel, or Networking, it generates a refined, optimized search query.

### 2. 🔍 The Retriever Node
*   **Services**: Qdrant Cloud (Vector Search) + FlashRank (Local Semantic Reranker)
*   **Mechanics: The Two-Stage Retrieval Pipeline**:
    *   **Stage 1 - Fast Bi-Encoder Retrieval (Qdrant)**:
        *   We convert the user query into a 3072-dimensional vector using Gemini's `gemini-embedding-2-preview`.
        *   We perform a **Cosine Similarity** search in Qdrant to find the top **15** candidates.
        *   *Why?* This is extremely fast (sub-10ms) because it only compares pre-calculated vectors. However, it lacks deep semantic understanding of the relationship between the query and the text.
    *   **Stage 2 - Deep Cross-Encoder Reranking (FlashRank)**:
        *   The top 15 candidates are passed to **FlashRank**, which uses a **Cross-Encoder** model (`ms-marco-MiniLM-L-6-v2`).
        *   Unlike the Bi-Encoder, the Cross-Encoder processes the query and the document *together* at the same time, allowing it to understand nuances like negation, complex relationships, and technical context.
        *   *Why FlashRank?* Normally, Cross-Encoders are heavy and expensive. FlashRank uses highly optimized ONNX models that run **locally on your CPU** with almost zero latency, providing "Gold Standard" reranking without any extra API costs.
    *   **Final Output**: Only the top **5** reranked documents are sent to the LLM. This ensures the LLM receives the most concentrated, high-signal information possible.
    *   **Zero-Downtime Fallback**: If the FlashRank model fails to load or errors out, the node gracefully falls back to the original Qdrant scores.

### 3. ✍️ The Responder Node
*   **Model**: Groq (Llama 3.3 70B)
*   **Logic**: This is the final synthesizer. It takes the retrieved documents (if any) and the conversation history to generate a natural, helpful response. 
*   **Sources**: It is instructed to cite its sources and use only the provided context for technical answers.

---

## ⛓️ Workflow Visualization

```mermaid
graph TD
    Start((Start)) --> Planner[Planner]
    Planner -->|Technical Query| Retriever[Retriever]
    Planner -->|Greeting/History| Skip((Skip Search))
    Retriever --> Rerank[FlashRank]
    Rerank --> Responder[Responder]
    Skip --> Responder
    Responder --> End((End))
```

---

## 💾 State & Memory
*   **Memory**: The graph uses `MemorySaver`. This allows the agent to maintain a "thread" of conversation. Even if the backend restarts, the agent can recall previous turns if the same `thread_id` is used.
*   **State**: The `AgentState` object tracks:
    *   `messages`: The full chat history.
    *   `current_query`: The optimized search term.
    *   `documents`: The reranked technical context.
    *   `plan`: A log of "thought steps" displayed in the UI.
