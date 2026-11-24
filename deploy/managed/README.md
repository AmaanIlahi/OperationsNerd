# Free managed deployment

Use the app on Render Free with its separate Neon Free PostgreSQL17 project and private Neon backup bucket. The default Dockerfile builds the complete app. This is the selected cloud profile; `../supabase` is a separate local/self-hosted fallback and its paid Blueprint must not be applied for this profile.

Database connections use direct PostgreSQL and certificate-verified TLS. Set `sslmode=verify-full&sslrootcert=/etc/ssl/certs/ca-certificates.crt` in the Linux URL. Keep credentials only in the hosting environment. The integration app retains account authentication and database-per-business routing. The standalone app sets HOST_PUBLIC_DEMO=true and shares fictional data without a login. Both external AI and carrier calls remain disabled.

BACKUP_S3_* settings enable durable archive upload before schema changes. An upload failure prevents backup completion and schema upgrade. Each archive has a SHA256 manifest. The private bucket survives app redeploys; it is in the same Neon project and is not an independent disaster-recovery provider. Local filesystem copies are temporary on Render Free.

Render free services sleep and share monthly runtime/build/transfer quotas. Neon Free also limits compute, storage and transfer. No paid plan or autoscaling upgrade is authorized. Keep synthetic demo records separate from real business data.
