-- Add generic measurement context tags to states (domain-agnostic condition scoping)
ALTER TABLE states ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}';
