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

-- Table: model_routes
-- Defines virtual model names and their ordered fallback chains.
-- Managed from Supabase directly — no deploy needed to add/change routes.
CREATE TABLE IF NOT EXISTS model_routes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    virtual_model TEXT NOT NULL,   -- The model name the client requests (e.g. 'combo-smart')
    provider TEXT NOT NULL,        -- Target provider (e.g. 'gemini', 'groq', 'openrouter')
    target_model TEXT NOT NULL,    -- Actual model name sent to the provider
    priority INTEGER NOT NULL DEFAULT 1, -- Lower = tried first in the fallback chain
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_model_routes_lookup
ON model_routes(virtual_model, enabled, priority)
WHERE enabled = TRUE;

-- Seed: default routes (mirrors the static fallback in router.py)
INSERT INTO model_routes (virtual_model, provider, target_model, priority) VALUES
    ('combo-smart',      'openrouter', 'anthropic/claude-3.5-sonnet',     1),
    ('combo-smart',      'gemini',     'gemini-1.5-pro',                  2),
    ('combo-fast',       'groq',       'llama3-8b-8192',                   1),
    ('combo-fast',       'gemini',     'gemini-1.5-flash',                 2),
    ('gemini-1.5-pro',   'gemini',     'gemini-1.5-pro',                   1),
    ('gemini-1.5-pro',   'openrouter', 'google/gemini-pro',                2),
    ('gemini-1.5-flash', 'gemini',     'gemini-1.5-flash',                 1),
    ('gemini-1.5-flash', 'openrouter', 'google/gemini-flash',              2),
    ('claude-3-5-sonnet','openrouter', 'anthropic/claude-3.5-sonnet',      1),
    ('claude-3-5-sonnet','gemini',     'gemini-1.5-pro',                   2),
    ('llama3-8b',        'groq',       'llama3-8b-8192',                   1),
    ('llama3-8b',        'openrouter', 'meta-llama/llama-3-8b-instruct',   2),
    ('gpt-4o',           'openai',     'gpt-4o',                          1),
    ('gpt-4o',           'openrouter', 'openai/gpt-4o',                   2),
    ('gpt-4o-mini',      'openai',     'gpt-4o-mini',                     1),
    ('gpt-4o-mini',      'openrouter', 'openai/gpt-4o-mini',              2),
    ('kimi-latest',      'moonshot',   'kimi-latest',                     1),
    ('kimi-latest',      'openrouter', 'moonshotai/kimi-latest',            2),
    ('moonshot-v1-8k',   'moonshot',   'moonshot-v1-8k',                  1),
    ('moonshot-v1-8k',   'openrouter', 'moonshotai/moonshot-v1-8k',         2)
ON CONFLICT DO NOTHING;

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

-- RPC: atomically increment error_count for a key
-- Called by pool_manager.py on every cooldown or dead event
CREATE OR REPLACE FUNCTION increment_error_count(key_id UUID)
RETURNS VOID AS $$
BEGIN
    UPDATE provider_keys
    SET error_count = error_count + 1
    WHERE id = key_id;
END;
$$ LANGUAGE plpgsql;
