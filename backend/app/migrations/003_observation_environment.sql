ALTER TABLE observations ADD COLUMN environment TEXT NOT NULL DEFAULT 'unknown' CHECK(environment IN ('unknown','indoor','outdoor'));
