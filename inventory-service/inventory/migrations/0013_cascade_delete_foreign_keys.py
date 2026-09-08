from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0012_alter_reservation_transaction_id'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            DO $$
            DECLARE
                r RECORD;
            BEGIN
                -- 1. Cascade delete for inventory_reservation -> inventory_seat
                FOR r IN (
                    SELECT conname
                    FROM pg_constraint
                    WHERE contype = 'f'
                      AND conrelid = 'inventory_reservation'::regclass
                      AND confrelid = 'inventory_seat'::regclass
                ) LOOP
                    EXECUTE 'ALTER TABLE inventory_reservation DROP CONSTRAINT ' || quote_ident(r.conname);
                    EXECUTE 'ALTER TABLE inventory_reservation ADD CONSTRAINT ' || quote_ident(r.conname) || ' FOREIGN KEY (seat_id) REFERENCES inventory_seat(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED';
                END LOOP;

                -- 2. Cascade delete for inventory_seat -> inventory_event
                FOR r IN (
                    SELECT conname
                    FROM pg_constraint
                    WHERE contype = 'f'
                      AND conrelid = 'inventory_seat'::regclass
                      AND confrelid = 'inventory_event'::regclass
                ) LOOP
                    EXECUTE 'ALTER TABLE inventory_seat DROP CONSTRAINT ' || quote_ident(r.conname);
                    EXECUTE 'ALTER TABLE inventory_seat ADD CONSTRAINT ' || quote_ident(r.conname) || ' FOREIGN KEY (event_id) REFERENCES inventory_event(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED';
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
                      AND conrelid = 'inventory_reservation'::regclass
                      AND confrelid = 'inventory_seat'::regclass
                ) LOOP
                    EXECUTE 'ALTER TABLE inventory_reservation DROP CONSTRAINT ' || quote_ident(r.conname);
                    EXECUTE 'ALTER TABLE inventory_reservation ADD CONSTRAINT ' || quote_ident(r.conname) || ' FOREIGN KEY (seat_id) REFERENCES inventory_seat(id) DEFERRABLE INITIALLY DEFERRED';
                END LOOP;

                FOR r IN (
                    SELECT conname
                    FROM pg_constraint
                    WHERE contype = 'f'
                      AND conrelid = 'inventory_seat'::regclass
                      AND confrelid = 'inventory_event'::regclass
                ) LOOP
                    EXECUTE 'ALTER TABLE inventory_seat DROP CONSTRAINT ' || quote_ident(r.conname);
                    EXECUTE 'ALTER TABLE inventory_seat ADD CONSTRAINT ' || quote_ident(r.conname) || ' FOREIGN KEY (event_id) REFERENCES inventory_event(id) DEFERRABLE INITIALLY DEFERRED';
                END LOOP;
            END $$;
            """
        ),
    ]
