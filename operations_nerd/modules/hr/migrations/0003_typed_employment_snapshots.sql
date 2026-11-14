-- CHECK treats SQL NULL as unknown/passing. Coalesce explicitly so a raw writer
-- cannot supply JSON null/missing state fields and bypass transition comparisons.
ALTER TABLE hr.employment_events ADD CONSTRAINT hr_snapshot_typed_fields CHECK(COALESCE(
 jsonb_typeof(snapshot->'status')='string' AND snapshot->>'status' IN('active','terminated')
 AND jsonb_typeof(snapshot->'employment_type')='string'
 AND snapshot->>'employment_type' IN('full_time','part_time','contractor','temporary','intern')
 AND jsonb_typeof(snapshot->'job_title')='string' AND length(trim(snapshot->>'job_title'))>0
 AND jsonb_typeof(snapshot->'start_date')='string' AND jsonb_typeof(snapshot->'employment_start_date')='string'
 AND jsonb_typeof(snapshot->'end_date') IN('string','null')
 AND ((snapshot->>'status'='terminated')=(jsonb_typeof(snapshot->'end_date')='string'))
 AND jsonb_typeof(snapshot->'location_record_id') IN('number','null')
 AND jsonb_typeof(snapshot->'department') IN('string','null')
 AND snapshot-ARRAY['status','employment_type','job_title','start_date','employment_start_date','end_date','location_record_id','department']='{}'::jsonb,
 false));
