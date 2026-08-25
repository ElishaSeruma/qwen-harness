begin;

create extension if not exists vector;

create table if not exists chats (
  id uuid primary key default gen_random_uuid(),
  agent_id text not null,
  agent_name text,
  title text,
  model_key text not null,
  context_limit integer check (context_limit is null or context_limit > 0),
  current_tokens integer not null default 0 check (current_tokens >= 0),
  last_compacted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists chat_messages (
  id uuid primary key default gen_random_uuid(),
  chat_id uuid not null references chats (id) on delete cascade,
  role text not null check (role in ('system', 'user', 'assistant', 'tool')),
  content text,
  tool_calls jsonb,
  tool_call_id text,
  tool_name text,
  token_count integer check (token_count is null or token_count >= 0),
  created_at timestamptz not null default now()
);

create table if not exists memory_matrix (
  id uuid primary key default gen_random_uuid(),
  chat_id uuid references chats (id) on delete set null,
  key text not null,
  value text not null,
  category text not null default 'fact',
  embedding vector(768),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists compacted_context (
  id uuid primary key default gen_random_uuid(),
  chat_id uuid not null references chats (id) on delete cascade,
  summary_text text not null,
  covers_up_to_message_id uuid references chat_messages (id) on delete set null,
  token_count_before integer check (token_count_before is null or token_count_before >= 0),
  token_count_after integer check (token_count_after is null or token_count_after >= 0),
  created_at timestamptz not null default now()
);

create table if not exists work_done (
  id uuid primary key default gen_random_uuid(),
  chat_id uuid references chats (id) on delete set null,
  agent_id text not null,
  action_type text not null check (action_type in ('create', 'edit', 'delete', 'read')),
  file_path text not null,
  summary text,
  created_at timestamptz not null default now()
);

create index if not exists idx_chats_agent_updated on chats (agent_id, updated_at desc);
create index if not exists idx_chat_messages_chat_created on chat_messages (chat_id, created_at);
create index if not exists idx_memory_matrix_chat_id on memory_matrix (chat_id) where chat_id is not null;
create index if not exists idx_memory_matrix_key on memory_matrix (key);
create index if not exists idx_memory_matrix_category on memory_matrix (category);
create index if not exists idx_memory_matrix_embedding on memory_matrix using hnsw (embedding vector_cosine_ops) where embedding is not null;
create index if not exists idx_compacted_context_chat_created on compacted_context (chat_id, created_at desc);
create index if not exists idx_compacted_context_message on compacted_context (covers_up_to_message_id) where covers_up_to_message_id is not null;
create index if not exists idx_work_done_chat_id on work_done (chat_id) where chat_id is not null;
create index if not exists idx_work_done_file_path on work_done (file_path);

create or replace function match_memory(query_embedding vector(768), match_count integer default 10)
returns table (id uuid, key text, value text, category text, chat_id uuid, created_at timestamptz, similarity double precision)
language sql stable security invoker set search_path = public as $$
  select m.id, m.key, m.value, m.category, m.chat_id, m.created_at,
         (1 - (m.embedding <=> query_embedding))::double precision
  from memory_matrix m
  where m.embedding is not null
  order by m.embedding <=> query_embedding
  limit greatest(0, least(match_count, 100));
$$;

create or replace function set_updated_at() returns trigger
language plpgsql set search_path = public as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_chats_updated_at on chats;
create trigger trg_chats_updated_at before update on chats for each row execute function set_updated_at();
drop trigger if exists trg_memory_matrix_updated_at on memory_matrix;
create trigger trg_memory_matrix_updated_at before update on memory_matrix for each row execute function set_updated_at();

alter table chats enable row level security;
alter table chat_messages enable row level security;
alter table memory_matrix enable row level security;
alter table compacted_context enable row level security;
alter table work_done enable row level security;

commit;
