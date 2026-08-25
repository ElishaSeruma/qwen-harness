begin;

create table if not exists projects (
  id text primary key,
  name text not null,
  working_directory text not null,
  repository jsonb not null default '{}'::jsonb,
  settings jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists agent_runs (
  id uuid primary key,
  project_id text references projects (id) on delete set null,
  parent_run_id uuid references agent_runs (id) on delete set null,
  surface text not null,
  instruction text not null,
  status text not null check (status in ('queued','planning','running','waiting_permission','verifying','completed','failed','cancelled','interrupted')),
  cwd text,
  model_key text,
  agent_ids jsonb not null default '[]'::jsonb,
  attempt integer not null default 1 check (attempt > 0),
  result jsonb,
  error text,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  completed_at timestamptz,
  updated_at timestamptz not null default now()
);

create table if not exists run_tasks (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references agent_runs (id) on delete cascade,
  task_key text not null,
  title text not null,
  status text not null,
  agent_id text,
  depends_on jsonb not null default '[]'::jsonb,
  result jsonb,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (run_id, task_key)
);

create table if not exists run_events (
  id bigint generated always as identity primary key,
  run_id uuid not null references agent_runs (id) on delete cascade,
  event_type text not null,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists approvals (
  id uuid primary key default gen_random_uuid(),
  run_id uuid references agent_runs (id) on delete cascade,
  scope text not null,
  request jsonb not null default '{}'::jsonb,
  decision text not null default 'pending' check (decision in ('pending','approved','denied','expired')),
  decided_at timestamptz,
  created_at timestamptz not null default now()
);

create table if not exists run_checkpoints (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references agent_runs (id) on delete cascade,
  kind text not null,
  git_head text,
  git_diff text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists project_documents (
  id uuid primary key default gen_random_uuid(),
  project_id text not null references projects (id) on delete cascade,
  name text not null,
  content_sha256 text not null,
  size_bytes bigint not null check (size_bytes >= 0),
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (project_id, name)
);

create index if not exists idx_agent_runs_project_updated on agent_runs (project_id, updated_at desc) where project_id is not null;
create index if not exists idx_agent_runs_status_updated on agent_runs (status, updated_at desc);
create index if not exists idx_agent_runs_parent on agent_runs (parent_run_id) where parent_run_id is not null;
create index if not exists idx_run_tasks_run_status on run_tasks (run_id, status);
create index if not exists idx_run_events_run_id on run_events (run_id, id);
create index if not exists idx_approvals_run_decision on approvals (run_id, decision) where run_id is not null;
create index if not exists idx_run_checkpoints_run_created on run_checkpoints (run_id, created_at desc);
create index if not exists idx_project_documents_project on project_documents (project_id, updated_at desc);

drop trigger if exists trg_projects_updated_at on projects;
create trigger trg_projects_updated_at before update on projects for each row execute function set_updated_at();
drop trigger if exists trg_agent_runs_updated_at on agent_runs;
create trigger trg_agent_runs_updated_at before update on agent_runs for each row execute function set_updated_at();
drop trigger if exists trg_run_tasks_updated_at on run_tasks;
create trigger trg_run_tasks_updated_at before update on run_tasks for each row execute function set_updated_at();
drop trigger if exists trg_project_documents_updated_at on project_documents;
create trigger trg_project_documents_updated_at before update on project_documents for each row execute function set_updated_at();

alter table projects enable row level security;
alter table agent_runs enable row level security;
alter table run_tasks enable row level security;
alter table run_events enable row level security;
alter table approvals enable row level security;
alter table run_checkpoints enable row level security;
alter table project_documents enable row level security;

commit;
