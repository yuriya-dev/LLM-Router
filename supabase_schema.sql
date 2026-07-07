-- supabase_schema.sql
-- Run this in your Supabase SQL Editor to create the necessary tables.

-- Create enum for provider status
CREATE TYPE key_status AS ENUM ('healthy', 'cooldown', 'dead');

-- Table: provider_keys
CREATE TABLE IF NOT EXISTS provider_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider TEXT NOT NULL, -- 'gemini', 'groq', 'openrouter', etc.
    key_name TEXT, -- Optional label for the account/key
    key_value TEXT NOT NULL UNIQUE, -- The API key
    status key_status NOT NULL DEFAULT 'healthy',
    cooldown_until TIMESTAMPTZ, -- If status is 'cooldown', until when
    priority INTEGER NOT NULL DEFAULT 1, -- Lower number = higher priority
    last_used_at TIMESTAMPTZ,
    error_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for fast lookup of healthy keys by provider
CREATE INDEX IF NOT EXISTS idx_provider_keys_routing 
ON provider_keys(provider, status, priority) 
WHERE status = 'healthy';

-- Table: request_logs
CREATE TABLE IF NOT EXISTS request_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    key_id UUID REFERENCES provider_keys(id) ON DELETE SET NULL,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    latency_ms INTEGER,
    status_code INTEGER,
    error_message TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for usage history reports
CREATE INDEX IF NOT EXISTS idx_request_logs_created_at ON request_logs(created_at DESC);

-- Trigger to auto-update updated_at on provider_keys
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER trigger_update_provider_keys_updated_at
BEFORE UPDATE ON provider_keys
FOR EACH ROW
EXECUTE FUNCTION update_updated_at_column();
