--
-- PostgreSQL database dump
--

\restrict 9cXjpLp7ZjqsFhraev2eCPurb6TyBkjVGocpqYXdTmjnYnJ22uUz1jJkcSraIXp

-- Dumped from database version 16.14 (Debian 16.14-1.pgdg12+1)
-- Dumped by pg_dump version 16.14 (Debian 16.14-1.pgdg12+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: code_index; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA code_index;


--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: file; Type: TABLE; Schema: code_index; Owner: -
--

CREATE TABLE code_index.file (
    id text NOT NULL,
    repository_id text NOT NULL,
    relative_path text NOT NULL,
    sha256 text NOT NULL,
    parse_status text NOT NULL,
    error_count bigint NOT NULL,
    updated_at timestamp with time zone NOT NULL,
    CONSTRAINT file_error_count_check CHECK ((error_count >= 0)),
    CONSTRAINT file_parse_status_check CHECK ((parse_status = ANY (ARRAY['parsed'::text, 'parsed-with-errors'::text])))
);


--
-- Name: relation; Type: TABLE; Schema: code_index; Owner: -
--

CREATE TABLE code_index.relation (
    id text NOT NULL,
    repository_id text NOT NULL,
    file_id text NOT NULL,
    source_symbol_id text,
    kind text NOT NULL,
    target_symbol_id text,
    target_name text,
    start_byte bigint NOT NULL,
    end_byte bigint NOT NULL,
    CONSTRAINT relation_check CHECK (((target_symbol_id IS NOT NULL) OR (target_name IS NOT NULL))),
    CONSTRAINT relation_kind_check CHECK ((kind = ANY (ARRAY['contains'::text, 'imports'::text, 'calls'::text, 'references'::text])))
);


--
-- Name: repository; Type: TABLE; Schema: code_index; Owner: -
--

CREATE TABLE code_index.repository (
    id text NOT NULL,
    name text NOT NULL,
    root_path text NOT NULL,
    language text NOT NULL,
    last_synced_at timestamp with time zone NOT NULL,
    CONSTRAINT repository_language_check CHECK ((language = 'rust'::text))
);


--
-- Name: symbol; Type: TABLE; Schema: code_index; Owner: -
--

CREATE TABLE code_index.symbol (
    id text NOT NULL,
    repository_id text NOT NULL,
    file_id text NOT NULL,
    kind text NOT NULL,
    name text NOT NULL,
    qualified_name text NOT NULL,
    start_byte bigint NOT NULL,
    end_byte bigint NOT NULL,
    start_line bigint NOT NULL,
    end_line bigint NOT NULL
);


--
-- Name: chunk; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chunk (
    id text NOT NULL,
    source_path text NOT NULL,
    content text DEFAULT ''::text NOT NULL,
    embedding public.vector(1024),
    origin text DEFAULT ''::text NOT NULL,
    project text DEFAULT ''::text NOT NULL,
    kind text DEFAULT ''::text NOT NULL,
    chunk_idx integer DEFAULT 0 NOT NULL,
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, content)) STORED
);


--
-- Name: claim; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.claim (
    subject text NOT NULL,
    predicate text NOT NULL,
    value text NOT NULL,
    source_path text NOT NULL,
    valid_from timestamp with time zone NOT NULL,
    superseded_at timestamp with time zone,
    embedding public.vector(1024),
    kind text DEFAULT 'fact'::text NOT NULL,
    confidence text DEFAULT 'certain'::text NOT NULL
);


--
-- Name: document; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document (
    source_path text NOT NULL,
    origin text DEFAULT ''::text NOT NULL,
    project text DEFAULT ''::text NOT NULL,
    kind text DEFAULT ''::text NOT NULL,
    title text,
    tags text[] DEFAULT '{}'::text[] NOT NULL,
    sha text DEFAULT ''::text NOT NULL,
    extracted_sha text DEFAULT ''::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: edge; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.edge (
    src text NOT NULL,
    dst text NOT NULL,
    kind text NOT NULL
);


--
-- Name: event_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.event_log (
    id bigint NOT NULL,
    observed_at timestamp with time zone DEFAULT now() NOT NULL,
    time_unix_nano bigint,
    severity_text text DEFAULT 'INFO'::text NOT NULL,
    severity_number integer DEFAULT 9 NOT NULL,
    service_name text DEFAULT ''::text NOT NULL,
    component text DEFAULT ''::text NOT NULL,
    event_name text DEFAULT ''::text NOT NULL,
    status text DEFAULT ''::text NOT NULL,
    trace_id text,
    span_id text,
    run_id text,
    session_id text,
    workflow text,
    workflow_node text,
    workflow_outcome text,
    body jsonb DEFAULT '{}'::jsonb NOT NULL,
    attributes jsonb DEFAULT '{}'::jsonb NOT NULL,
    resource jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: event_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.event_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: event_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.event_log_id_seq OWNED BY public.event_log.id;


--
-- Name: node; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.node (
    id text NOT NULL,
    kind text NOT NULL,
    label text DEFAULT ''::text NOT NULL,
    outcome text
);


--
-- Name: query_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.query_log (
    id integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    endpoint text NOT NULL,
    query text DEFAULT ''::text NOT NULL,
    hit_paths text[] DEFAULT '{}'::text[] NOT NULL,
    sources text[] DEFAULT '{}'::text[] NOT NULL,
    answer_snippet text DEFAULT ''::text NOT NULL,
    latency_ms integer,
    hit_dists real[] DEFAULT '{}'::real[] NOT NULL,
    hit_dist_kinds text[] DEFAULT '{}'::text[] NOT NULL
);


--
-- Name: query_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.query_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: query_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.query_log_id_seq OWNED BY public.query_log.id;


--
-- Name: recall_label; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.recall_label (
    query_log_id integer NOT NULL,
    hit_index integer NOT NULL,
    judge text NOT NULL,
    verdict text NOT NULL,
    model text DEFAULT ''::text NOT NULL,
    note text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: event_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_log ALTER COLUMN id SET DEFAULT nextval('public.event_log_id_seq'::regclass);


--
-- Name: query_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.query_log ALTER COLUMN id SET DEFAULT nextval('public.query_log_id_seq'::regclass);


--
-- Name: file file_pkey; Type: CONSTRAINT; Schema: code_index; Owner: -
--

ALTER TABLE ONLY code_index.file
    ADD CONSTRAINT file_pkey PRIMARY KEY (id);


--
-- Name: file file_repository_id_id_key; Type: CONSTRAINT; Schema: code_index; Owner: -
--

ALTER TABLE ONLY code_index.file
    ADD CONSTRAINT file_repository_id_id_key UNIQUE (repository_id, id);


--
-- Name: file file_repository_id_relative_path_key; Type: CONSTRAINT; Schema: code_index; Owner: -
--

ALTER TABLE ONLY code_index.file
    ADD CONSTRAINT file_repository_id_relative_path_key UNIQUE (repository_id, relative_path);


--
-- Name: relation relation_pkey; Type: CONSTRAINT; Schema: code_index; Owner: -
--

ALTER TABLE ONLY code_index.relation
    ADD CONSTRAINT relation_pkey PRIMARY KEY (id);


--
-- Name: relation relation_repository_id_id_key; Type: CONSTRAINT; Schema: code_index; Owner: -
--

ALTER TABLE ONLY code_index.relation
    ADD CONSTRAINT relation_repository_id_id_key UNIQUE (repository_id, id);


--
-- Name: repository repository_pkey; Type: CONSTRAINT; Schema: code_index; Owner: -
--

ALTER TABLE ONLY code_index.repository
    ADD CONSTRAINT repository_pkey PRIMARY KEY (id);


--
-- Name: symbol symbol_pkey; Type: CONSTRAINT; Schema: code_index; Owner: -
--

ALTER TABLE ONLY code_index.symbol
    ADD CONSTRAINT symbol_pkey PRIMARY KEY (id);


--
-- Name: symbol symbol_repository_id_id_key; Type: CONSTRAINT; Schema: code_index; Owner: -
--

ALTER TABLE ONLY code_index.symbol
    ADD CONSTRAINT symbol_repository_id_id_key UNIQUE (repository_id, id);


--
-- Name: chunk chunk_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk
    ADD CONSTRAINT chunk_pkey PRIMARY KEY (id);


--
-- Name: claim claim_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.claim
    ADD CONSTRAINT claim_pkey PRIMARY KEY (subject, predicate, valid_from);


--
-- Name: document document_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document
    ADD CONSTRAINT document_pkey PRIMARY KEY (source_path);


--
-- Name: edge edge_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.edge
    ADD CONSTRAINT edge_pkey PRIMARY KEY (src, dst, kind);


--
-- Name: event_log event_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_log
    ADD CONSTRAINT event_log_pkey PRIMARY KEY (id);


--
-- Name: node node_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.node
    ADD CONSTRAINT node_pkey PRIMARY KEY (id);


--
-- Name: query_log query_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.query_log
    ADD CONSTRAINT query_log_pkey PRIMARY KEY (id);


--
-- Name: recall_label recall_label_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recall_label
    ADD CONSTRAINT recall_label_pkey PRIMARY KEY (query_log_id, hit_index, judge);


--
-- Name: code_index_file_repository; Type: INDEX; Schema: code_index; Owner: -
--

CREATE INDEX code_index_file_repository ON code_index.file USING btree (repository_id);


--
-- Name: code_index_relation_repository_kind; Type: INDEX; Schema: code_index; Owner: -
--

CREATE INDEX code_index_relation_repository_kind ON code_index.relation USING btree (repository_id, kind);


--
-- Name: code_index_symbol_repository_name; Type: INDEX; Schema: code_index; Owner: -
--

CREATE INDEX code_index_symbol_repository_name ON code_index.symbol USING btree (repository_id, name);


--
-- Name: chunk_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunk_gin ON public.chunk USING gin (tsv);


--
-- Name: chunk_hnsw; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunk_hnsw ON public.chunk USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: claim_current; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX claim_current ON public.claim USING btree (subject, predicate) WHERE (superseded_at IS NULL);


--
-- Name: claim_hnsw; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX claim_hnsw ON public.claim USING hnsw (embedding public.vector_cosine_ops) WHERE (superseded_at IS NULL);


--
-- Name: claim_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX claim_kind ON public.claim USING btree (kind) WHERE (superseded_at IS NULL);


--
-- Name: document_updated; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_updated ON public.document USING btree (updated_at DESC);


--
-- Name: edge_dst; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX edge_dst ON public.edge USING btree (dst);


--
-- Name: edge_src; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX edge_src ON public.edge USING btree (src);


--
-- Name: event_log_component; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX event_log_component ON public.event_log USING btree (component, observed_at DESC);


--
-- Name: event_log_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX event_log_event ON public.event_log USING btree (event_name, observed_at DESC);


--
-- Name: event_log_observed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX event_log_observed ON public.event_log USING btree (observed_at DESC, id DESC);


--
-- Name: event_log_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX event_log_run_id ON public.event_log USING btree (run_id, observed_at DESC);


--
-- Name: event_log_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX event_log_status ON public.event_log USING btree (status, observed_at DESC);


--
-- Name: query_log_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX query_log_created ON public.query_log USING btree (created_at DESC);


--
-- Name: recall_label_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX recall_label_created ON public.recall_label USING btree (created_at DESC);


--
-- Name: file file_repository_id_fkey; Type: FK CONSTRAINT; Schema: code_index; Owner: -
--

ALTER TABLE ONLY code_index.file
    ADD CONSTRAINT file_repository_id_fkey FOREIGN KEY (repository_id) REFERENCES code_index.repository(id) ON DELETE CASCADE;


--
-- Name: relation relation_file_id_fkey; Type: FK CONSTRAINT; Schema: code_index; Owner: -
--

ALTER TABLE ONLY code_index.relation
    ADD CONSTRAINT relation_file_id_fkey FOREIGN KEY (file_id) REFERENCES code_index.file(id) ON DELETE CASCADE;


--
-- Name: relation relation_repository_file_fkey; Type: FK CONSTRAINT; Schema: code_index; Owner: -
--

ALTER TABLE ONLY code_index.relation
    ADD CONSTRAINT relation_repository_file_fkey FOREIGN KEY (repository_id, file_id) REFERENCES code_index.file(repository_id, id) ON DELETE CASCADE;


--
-- Name: relation relation_repository_id_fkey; Type: FK CONSTRAINT; Schema: code_index; Owner: -
--

ALTER TABLE ONLY code_index.relation
    ADD CONSTRAINT relation_repository_id_fkey FOREIGN KEY (repository_id) REFERENCES code_index.repository(id) ON DELETE CASCADE;


--
-- Name: relation relation_repository_source_symbol_fkey; Type: FK CONSTRAINT; Schema: code_index; Owner: -
--

ALTER TABLE ONLY code_index.relation
    ADD CONSTRAINT relation_repository_source_symbol_fkey FOREIGN KEY (repository_id, source_symbol_id) REFERENCES code_index.symbol(repository_id, id) ON DELETE CASCADE;


--
-- Name: relation relation_repository_target_symbol_fkey; Type: FK CONSTRAINT; Schema: code_index; Owner: -
--

ALTER TABLE ONLY code_index.relation
    ADD CONSTRAINT relation_repository_target_symbol_fkey FOREIGN KEY (repository_id, target_symbol_id) REFERENCES code_index.symbol(repository_id, id) ON DELETE CASCADE;


--
-- Name: relation relation_source_symbol_id_fkey; Type: FK CONSTRAINT; Schema: code_index; Owner: -
--

ALTER TABLE ONLY code_index.relation
    ADD CONSTRAINT relation_source_symbol_id_fkey FOREIGN KEY (source_symbol_id) REFERENCES code_index.symbol(id) ON DELETE CASCADE;


--
-- Name: relation relation_target_symbol_id_fkey; Type: FK CONSTRAINT; Schema: code_index; Owner: -
--

ALTER TABLE ONLY code_index.relation
    ADD CONSTRAINT relation_target_symbol_id_fkey FOREIGN KEY (target_symbol_id) REFERENCES code_index.symbol(id) ON DELETE SET NULL;


--
-- Name: symbol symbol_file_id_fkey; Type: FK CONSTRAINT; Schema: code_index; Owner: -
--

ALTER TABLE ONLY code_index.symbol
    ADD CONSTRAINT symbol_file_id_fkey FOREIGN KEY (file_id) REFERENCES code_index.file(id) ON DELETE CASCADE;


--
-- Name: symbol symbol_repository_file_fkey; Type: FK CONSTRAINT; Schema: code_index; Owner: -
--

ALTER TABLE ONLY code_index.symbol
    ADD CONSTRAINT symbol_repository_file_fkey FOREIGN KEY (repository_id, file_id) REFERENCES code_index.file(repository_id, id) ON DELETE CASCADE;


--
-- Name: symbol symbol_repository_id_fkey; Type: FK CONSTRAINT; Schema: code_index; Owner: -
--

ALTER TABLE ONLY code_index.symbol
    ADD CONSTRAINT symbol_repository_id_fkey FOREIGN KEY (repository_id) REFERENCES code_index.repository(id) ON DELETE CASCADE;


--
-- Name: chunk chunk_source_path_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunk
    ADD CONSTRAINT chunk_source_path_fkey FOREIGN KEY (source_path) REFERENCES public.document(source_path) ON DELETE CASCADE;


--
-- Name: recall_label recall_label_query_log_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recall_label
    ADD CONSTRAINT recall_label_query_log_id_fkey FOREIGN KEY (query_log_id) REFERENCES public.query_log(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict 9cXjpLp7ZjqsFhraev2eCPurb6TyBkjVGocpqYXdTmjnYnJ22uUz1jJkcSraIXp

