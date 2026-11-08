-- More kinds of recorded evidence (PRD FR-034 owner approvals, FR-038 change-record attachments, FR-044 reviewer verdicts, FR-045 team
-- notifications and acknowledgements, FR-031 incidents).
alter table evidence_records drop constraint evidence_records_kind_check;
alter table evidence_records add constraint evidence_records_kind_check check (kind in (
    'test_run', 'coverage', 'runtime_edge', 'deployment', 'incident', 'verdict',
    'approval', 'acknowledgement', 'notification', 'itsm_attachment'));
create index evidence_by_analysis on evidence_records (tenant_id, kind, (data->>'analysis_id'));
