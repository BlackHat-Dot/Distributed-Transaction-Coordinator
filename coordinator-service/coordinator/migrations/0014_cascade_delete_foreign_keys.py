from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('coordinator', '0013_remove_transaction_seat_id_transaction_seat_ids_and_more'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            DO $$
            DECLARE
                r RECORD;
            BEGIN
                FOR r IN (
                    SELECT conname
                    FROM pg_constraint
                    WHERE contype = 'f'
                      AND conrelid = 'coordinator_sagaevent'::regclass
                      AND confrelid = 'coordinator_transaction'::regclass
                ) LOOP
                    EXECUTE 'ALTER TABLE coordinator_sagaevent DROP CONSTRAINT ' || quote_ident(r.conname);
                    EXECUTE 'ALTER TABLE coordinator_sagaevent ADD CONSTRAINT ' || quote_ident(r.conname) || ' FOREIGN KEY (transaction_id) REFERENCES coordinator_transaction(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED';
                END LOOP;
            END $$;
            """,
            reverse_sql="""
            DO $$
            DECLARE
                r RECORD;
            BEGIN
                FOR r IN (
                    SELECT conname
                    FROM pg_constraint
                    WHERE contype = 'f'
                      AND conrelid = 'coordinator_sagaevent'::regclass
                      AND confrelid = 'coordinator_transaction'::regclass
                ) LOOP
                    EXECUTE 'ALTER TABLE coordinator_sagaevent DROP CONSTRAINT ' || quote_ident(r.conname);
                    EXECUTE 'ALTER TABLE coordinator_sagaevent ADD CONSTRAINT ' || quote_ident(r.conname) || ' FOREIGN KEY (transaction_id) REFERENCES coordinator_transaction(id) DEFERRABLE INITIALLY DEFERRED';
                END LOOP;
            END $$;
            """
        ),
    ]
