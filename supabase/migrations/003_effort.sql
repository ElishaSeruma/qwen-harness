begin;

alter table agent_runs add column if not exists effort_level smallint not null default 1;
alter table agent_runs add column if not exists effort_source text not null default 'auto';
alter table agent_runs add column if not exists effort_reason text;
alter table agent_runs add column if not exists effort_escalated_at timestamptz;

alter table agent_runs drop constraint if exists agent_runs_effort_level_check;
alter table agent_runs add constraint agent_runs_effort_level_check check (effort_level between 1 and 4);
alter table agent_runs drop constraint if exists agent_runs_effort_source_check;
alter table agent_runs add constraint agent_runs_effort_source_check check (effort_source in ('auto', 'manual', 'model'));

create index if not exists idx_agent_runs_effort on agent_runs (effort_level, updated_at desc);

commit;
