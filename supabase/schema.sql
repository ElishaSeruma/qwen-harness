-- qwen-harness legacy single-file bootstrap.
--
-- The authoritative, production schema is versioned in supabase/migrations/.
-- Apply every numbered migration in supabase/migrations in order. This file remains for
-- compatibility with earlier installs and should not be used for new setup.
--
-- Run this yourself in the Supabase SQL editor (or `supabase db execute`)
-- once we've confirmed the design. Nothing in this repo runs it automatically.
--
-- Access model: these tables are reached ONLY via gui/server.py using the
-- service_role key (stored locally at ~/.qwen-harness/secrets.json, never
-- sent to the browser). RLS is left OFF below since the browser never talks
-- to Supabase directly — if that changes later (e.g. a hosted deployment),
-- RLS policies should be added before exposing these tables to any
-- anon-key/browser-side client.

-- ── chats ────────────────────────────────────────────────────────────────
-- One row per conversation. `current_tokens` is a running tally (from each
-- Ollama response's usage.total_tokens) used to trigger auto-compaction.
create table if not exists chats (
  id uuid primary key default gen_random_uuid(),
  agent_id text not null,           -- matches the GUI's agent.id (e.g. "agent-1")
  agent_name text,                  -- denormalized for display without a join
  title text,                       -- auto-derived from the first user message
  model_key text not null,
  context_limit integer,            -- the model's context window at chat-creation time
  current_tokens integer not null default 0,
  last_compacted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_chats_agent_id on chats (agent_id);
create index if not exists idx_chats_updated_at on chats (updated_at desc);

-- ── chat_messages ────────────────────────────────────────────────────────
-- Full message history per chat — what "continue where you left off" replays.
create table if not exists chat_messages (
  id uuid primary key default gen_random_uuid(),
  chat_id uuid not null references chats (id) on delete cascade,
  role text not null check (role in ('system', 'user', 'assistant', 'tool')),
  content text,
  tool_calls jsonb,                 -- present on assistant messages that called tools
  tool_call_id text,                -- present on role='tool' messages
  tool_name text,                   -- present on tool_call/tool_result entries, for display
  token_count integer,
  created_at timestamptz not null default now()
);
create index if not exists idx_chat_messages_chat_id on chat_messages (chat_id, created_at);

-- ── memory_matrix ────────────────────────────────────────────────────────
-- Chunked, contextualized memory. `chat_id` is nullable: some entries are
-- global (user preferences), others scoped to one chat.
--
-- `embedding` is included for future semantic search (pgvector) but is NOT
-- populated yet — that needs an embedding model wired up first (e.g. Ollama
-- serving nomic-embed-text). Leave this column unused until we confirm you
-- want that; keyword search (memory_search, already built) works without it.
create extension if not exists vector;

create table if not exists memory_matrix (
  id uuid primary key default gen_random_uuid(),
  chat_id uuid references chats (id) on delete set null,
  key text not null,
  value text not null,
  category text not null default 'fact',   -- fact | preference | user_edit | ...
  embedding vector(768),                    -- nullable; phase 2
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_memory_matrix_key on memory_matrix (key);
create index if not exists idx_memory_matrix_category on memory_matrix (category);

-- ivfflat index for fast approximate nearest-neighbor search once there's
-- enough data (pgvector recommends building this after some rows exist;
-- harmless to create early on an empty/small table too).
create index if not exists idx_memory_matrix_embedding on memory_matrix
  using ivfflat (embedding vector_cosine_ops) with (lists = 100);

-- RPC used by tools/supabase_client.py's semantic search path (cosine
-- distance via pgvector's <=> operator — smaller is more similar).
create or replace function match_memory(query_embedding vector(768), match_count int default 10)
returns table (
  id uuid, key text, value text, category text, chat_id uuid,
  created_at timestamptz, similarity float
) language sql stable as $$
  select id, key, value, category, chat_id, created_at,
         1 - (embedding <=> query_embedding) as similarity
  from memory_matrix
  where embedding is not null
  order by embedding <=> query_embedding
  limit match_count;
$$;

-- ── compacted_context ────────────────────────────────────────────────────
-- When a chat's current_tokens crosses the compaction threshold, older
-- messages get summarized into one of these instead of being deleted.
create table if not exists compacted_context (
  id uuid primary key default gen_random_uuid(),
  chat_id uuid not null references chats (id) on delete cascade,
  summary_text text not null,
  covers_up_to_message_id uuid references chat_messages (id),
  token_count_before integer,
  token_count_after integer,
  created_at timestamptz not null default now()
);
create index if not exists idx_compacted_context_chat_id on compacted_context (chat_id, created_at);

-- ── work_done ────────────────────────────────────────────────────────────
-- Audit trail of file CRUD actions, linked back to the chat + agent that
-- caused them. Populated automatically from the tool-call loop.
create table if not exists work_done (
  id uuid primary key default gen_random_uuid(),
  chat_id uuid references chats (id) on delete set null,
  agent_id text not null,
  action_type text not null check (action_type in ('create', 'edit', 'delete', 'read')),
  file_path text not null,
  summary text,
  created_at timestamptz not null default now()
);
create index if not exists idx_work_done_chat_id on work_done (chat_id);
create index if not exists idx_work_done_file_path on work_done (file_path);

-- ── updated_at trigger (chats, memory_matrix) ───────────────────────────
create or replace function set_updated_at() returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_chats_updated_at on chats;
create trigger trg_chats_updated_at before update on chats
  for each row execute function set_updated_at();

drop trigger if exists trg_memory_matrix_updated_at on memory_matrix;
create trigger trg_memory_matrix_updated_at before update on memory_matrix
  for each row execute function set_updated_at();
