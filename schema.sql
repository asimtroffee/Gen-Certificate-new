-- Run this in the Supabase SQL Editor

-- 1. Create events table
CREATE TABLE IF NOT EXISTS public.events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name TEXT NOT NULL,
    template_storage_path TEXT,
    config_json JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Enable RLS for events
ALTER TABLE public.events ENABLE ROW LEVEL SECURITY;

-- Allow anonymous read access to events
CREATE POLICY "Allow public read access to events"
ON public.events FOR SELECT
TO public
USING (true);

-- Allow public insert/update (for the admin dashboard without auth, since it seems to be unauthenticated currently)
CREATE POLICY "Allow public insert to events"
ON public.events FOR INSERT
TO public
WITH CHECK (true);

CREATE POLICY "Allow public update to events"
ON public.events FOR UPDATE
TO public
USING (true);


-- 2. Create teacher_links table
CREATE TABLE IF NOT EXISTS public.teacher_links (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    token UUID NOT NULL DEFAULT uuid_generate_v4() UNIQUE,
    event_id UUID REFERENCES public.events(id) ON DELETE CASCADE,
    teacher_name TEXT NOT NULL,
    teacher_email TEXT NOT NULL,
    used BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Enable RLS for teacher_links
ALTER TABLE public.teacher_links ENABLE ROW LEVEL SECURITY;

-- Allow anonymous access to teacher_links
CREATE POLICY "Allow public read access to teacher_links"
ON public.teacher_links FOR SELECT
TO public
USING (true);

CREATE POLICY "Allow public insert to teacher_links"
ON public.teacher_links FOR INSERT
TO public
WITH CHECK (true);

CREATE POLICY "Allow public update to teacher_links"
ON public.teacher_links FOR UPDATE
TO public
USING (true);

-- Migration: Add school and completed_at to teacher_links (added 2026-07-21)
ALTER TABLE public.teacher_links ADD COLUMN IF NOT EXISTS school TEXT;
ALTER TABLE public.teacher_links ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP WITH TIME ZONE;


-- 3. Create certificates table
CREATE TABLE IF NOT EXISTS public.certificates (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name TEXT NOT NULL,
    email TEXT,
    batch_id TEXT NOT NULL,
    storage_path TEXT,
    format TEXT DEFAULT 'png',
    sent BOOLEAN DEFAULT FALSE,
    sent_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Enable RLS for certificates
ALTER TABLE public.certificates ENABLE ROW LEVEL SECURITY;

-- Allow anonymous access to certificates
CREATE POLICY "Allow public read access to certificates"
ON public.certificates FOR SELECT
TO public
USING (true);

CREATE POLICY "Allow public insert to certificates"
ON public.certificates FOR INSERT
TO public
WITH CHECK (true);

CREATE POLICY "Allow public update to certificates"
ON public.certificates FOR UPDATE
TO public
USING (true);
