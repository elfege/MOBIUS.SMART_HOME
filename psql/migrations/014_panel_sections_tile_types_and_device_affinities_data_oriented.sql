-- =============================================================================
-- 014_panel_sections_tile_types_and_device_affinities_data_oriented.sql
-- =============================================================================
-- THE PANEL'S ORGANIZING DATA — sections, tile-type resolution, and device
-- affinities. All four tables exist to answer questions that TILES answered in
-- JAVASCRIPT, and that is precisely the thing being corrected here.
--
-- OPERATOR DIRECTIVE (2026-07-13): "beware of data-oriented logic: everything
-- registered in tables especially affinities."
--
-- In TILES, two decisions were hard-coded in the frontend:
--   1. "which tile do I render for this device?"  -> an if/else chain over
--      capabilities inside the tile component;
--   2. "which room/section does this device belong to?" -> a hand-written
--      keyword list inside the auto-sectionizer.
-- Both are DATA, not code. Hard-coding them means: the operator cannot add a
-- room or re-map a capability without a code change + rebuild; the server
-- cannot answer "what does the panel look like?" without running JavaScript;
-- and two clients (web panel, native app) inevitably drift because each carries
-- its own copy of the chain.
--
-- Here the server resolves BOTH from these tables and ships an already-resolved
-- roster. The client renders what it is told. Adding a room, re-prioritizing a
-- capability, or pinning a device to a section is an INSERT/UPDATE — never a
-- deploy.
--
-- FK POLICY (2026-07-11 hard policy): no ON DELETE CASCADE anywhere. Affinity
-- rows reference dshub.devices(id) WITHOUT a foreign key on purpose:
--   - a CASCADE would silently delete an operator's careful placement when a
--     device blips out of a hub pull;
--   - a RESTRICT would make device removal fail because a panel remembers it.
-- A dangling affinity row is inert (the roster INNER JOINs live devices), and
-- deliberate cleanup belongs to a removal class, not to the storage engine.
-- Devices are NOT deleted on disappearance anyway — the classifier sets
-- is_present=false — so orphans are rare by design.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS dsapp;

-- -----------------------------------------------------------------------------
-- panel_sections — the rooms/groups a panel shows. Profile-scoped, because a
-- wall tablet is a PROFILE, not a person (MOBIUS.HOME has no user model).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dsapp.panel_sections (
    id          BIGSERIAL PRIMARY KEY,
    profile     TEXT        NOT NULL DEFAULT 'default',
    slug        TEXT        NOT NULL,          -- stable key: rules point at this
    name        TEXT        NOT NULL,          -- display label
    icon        TEXT,                          -- icon key the client maps to a glyph
    sort_order  INTEGER     NOT NULL DEFAULT 100,
    is_system   BOOLEAN     NOT NULL DEFAULT false,  -- seeded here vs operator-created
    is_hidden   BOOLEAN     NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_panel_sections_profile_slug UNIQUE (profile, slug)
);

COMMENT ON TABLE dsapp.panel_sections IS
    'Panel rooms/groups, profile-scoped. Operator-editable: adding a room is an '
    'INSERT, not a code change.';

-- -----------------------------------------------------------------------------
-- panel_tile_types — CAPABILITY -> TILE RENDERER. Replaces the if/else chain.
--
-- Resolution: a device may carry many capabilities (an RGBW bulb has Switch,
-- SwitchLevel, ColorControl, ColorTemperature). The tile is decided by the
-- LOWEST `priority` row among the device's capabilities: ColorControl (10)
-- outranks SwitchLevel (20) outranks Switch (30) — the richest control wins.
--
-- Capabilities are PascalCase in Hubitat; every comparison lowercases both
-- sides (known trap — see the capability-case-normalization ruling).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dsapp.panel_tile_types (
    id                 BIGSERIAL PRIMARY KEY,
    capability         TEXT        NOT NULL,   -- Hubitat capability, PascalCase
    tile_type          TEXT        NOT NULL,   -- renderer key the client implements
    priority           INTEGER     NOT NULL,   -- LOWER WINS among a device's caps
    primary_attribute  TEXT,                   -- attribute that drives tile state
    is_actionable      BOOLEAN     NOT NULL DEFAULT true,   -- false = display-only
    is_enabled         BOOLEAN     NOT NULL DEFAULT true,
    notes              TEXT,
    CONSTRAINT uq_panel_tile_types_capability UNIQUE (capability)
);

COMMENT ON TABLE dsapp.panel_tile_types IS
    'Capability -> tile renderer, as DATA. Lowest priority row among a device''s '
    'capabilities wins, so the richest control surfaces (color > dimmer > switch). '
    'A capability with NO row here is not tile-bearing (Actuator, Refresh, '
    'Configuration...), which is how utility capabilities are excluded without a '
    'blocklist in the client.';

-- -----------------------------------------------------------------------------
-- panel_section_rules — THE AUTO-SECTIONIZER, as rows.
-- Apple-style grouping: infer the room from the device's name/type/capability.
-- Lowest `priority` wins; ties broken by the LONGEST pattern (a rule matching
-- "living room" is more specific than one matching "room").
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dsapp.panel_section_rules (
    id            BIGSERIAL PRIMARY KEY,
    section_slug  TEXT        NOT NULL,   -- -> panel_sections.slug (resolved per profile)
    match_kind    TEXT        NOT NULL,   -- name_keyword | device_type | capability
    pattern       TEXT        NOT NULL,   -- compared case-insensitively
    priority      INTEGER     NOT NULL DEFAULT 100,
    is_enabled    BOOLEAN     NOT NULL DEFAULT true,
    CONSTRAINT chk_panel_section_rules_kind
        CHECK (match_kind IN ('name_keyword', 'device_type', 'capability'))
);

CREATE INDEX IF NOT EXISTS idx_panel_section_rules_enabled
    ON dsapp.panel_section_rules(is_enabled, priority);

COMMENT ON TABLE dsapp.panel_section_rules IS
    'Auto-sectionizer as data: infer a device''s room from its name/type/capability. '
    'The operator retunes grouping with an INSERT/UPDATE instead of editing JS.';

-- -----------------------------------------------------------------------------
-- panel_device_affinities — THE AFFINITY TABLE (the operator named this one).
--
-- An EXPLICIT operator placement, which always outranks the auto-sectionizer.
-- Also carries per-device overrides: a tile_type override (force a rich bulb to
-- render as a plain switch), display order, a custom label, and hiding.
-- One row per (profile, device) — absence means "auto".
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dsapp.panel_device_affinities (
    id             BIGSERIAL PRIMARY KEY,
    profile        TEXT        NOT NULL DEFAULT 'default',
    device_id      BIGINT      NOT NULL,   -- dshub.devices.id — NO FK, see header
    section_id     BIGINT,                 -- NULL = fall back to the auto rules
    tile_type      TEXT,                   -- NULL = resolve from panel_tile_types
    custom_label   TEXT,                   -- NULL = use the device's own label
    sort_order     INTEGER,
    is_hidden      BOOLEAN     NOT NULL DEFAULT false,
    is_favorite    BOOLEAN     NOT NULL DEFAULT false,  -- lands on the home grid
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_panel_device_affinities_profile_device UNIQUE (profile, device_id)
);

CREATE INDEX IF NOT EXISTS idx_panel_device_affinities_section
    ON dsapp.panel_device_affinities(profile, section_id);

COMMENT ON TABLE dsapp.panel_device_affinities IS
    'Explicit device->section placement + per-device tile overrides. Outranks the '
    'auto-sectionizer. No FK to devices BY POLICY (no CASCADE: a hub blip must '
    'never silently erase an operator''s placement).';

-- =============================================================================
-- SEED — the knowledge that used to live in TILES'' JavaScript.
-- =============================================================================

-- Sections (system-seeded; operator adds/renames freely afterwards).
INSERT INTO dsapp.panel_sections (profile, slug, name, icon, sort_order, is_system) VALUES
    ('default', 'favorites',  'Favorites',   'star',       10,  true),
    ('default', 'kitchen',    'Kitchen',     'kitchen',    20,  true),
    ('default', 'living',     'Living Room', 'sofa',       30,  true),
    ('default', 'dining',     'Dining Room', 'dining',     40,  true),
    ('default', 'bedroom',    'Bedroom',     'bed',        50,  true),
    ('default', 'office',     'Office',      'desk',       60,  true),
    ('default', 'bathroom',   'Bathroom',    'bath',       70,  true),
    ('default', 'hallway',    'Hallway',     'stairs',     80,  true),
    ('default', 'laundry',    'Laundry',     'laundry',    90,  true),
    ('default', 'basement',   'Basement',    'basement',  100,  true),
    ('default', 'garage',     'Garage',      'garage',    110,  true),
    ('default', 'outdoor',    'Outdoor',     'tree',      120,  true),
    ('default', 'climate',    'Climate',     'thermostat',130,  true),
    ('default', 'security',   'Security',    'shield',    140,  true),
    ('default', 'other',      'Other',       'dots',      900,  true)
ON CONFLICT (profile, slug) DO NOTHING;

-- Capability -> tile renderer. Priority: richest control wins.
INSERT INTO dsapp.panel_tile_types
    (capability, tile_type, priority, primary_attribute, is_actionable, notes) VALUES
    -- Rich controls (own modal / custom surface)
    ('Thermostat',                'thermostat',  5,  'temperature',    true,  'round slider + setpoints'),
    ('Lock',                      'lock',        6,  'lock',           true,  'animated lock'),
    ('GarageDoorControl',         'garage',      7,  'door',           true,  NULL),
    ('WindowShade',               'shade',       8,  'windowShade',    true,  NULL),
    ('Valve',                     'valve',       9,  'valve',          true,  NULL),
    ('ColorControl',              'color',      10,  'switch',         true,  'color wheel + presets; outranks dimmer'),
    ('ColorTemperature',          'color_temp', 15,  'switch',         true,  'CT slider (no full color wheel)'),
    ('FanControl',                'fan',        18,  'speed',          true,  NULL),
    ('SwitchLevel',               'dimmer',     20,  'switch',         true,  'vertical touch slider'),
    ('Outlet',                    'switch',     28,  'switch',         true,  NULL),
    ('Switch',                    'switch',     30,  'switch',         true,  'the floor for anything switchable'),
    ('MusicPlayer',               'media',      40,  'status',         true,  NULL),
    ('AudioVolume',               'media',      41,  'volume',         true,  NULL),
    ('PushableButton',            'button',     45,  'pushed',         true,  'tap/doubleTap/hold action matrix'),
    -- Display-only sensors (is_actionable = false: no command surface at all)
    ('PowerMeter',                'power',      50,  'power',          false, 'live power tile'),
    ('EnergyMeter',               'energy',     55,  'energy',         false, NULL),
    ('MotionSensor',              'motion',     60,  'motion',         false, NULL),
    ('ContactSensor',             'contact',    61,  'contact',        false, NULL),
    ('PresenceSensor',            'presence',   62,  'presence',       false, NULL),
    ('WaterSensor',               'water',      63,  'water',          false, 'alert tile'),
    ('SmokeDetector',             'smoke',      64,  'smoke',          false, 'alert tile'),
    ('TemperatureMeasurement',    'sensor',     70,  'temperature',    false, NULL),
    ('RelativeHumidityMeasurement','sensor',    71,  'humidity',       false, NULL),
    ('IlluminanceMeasurement',    'sensor',     72,  'illuminance',    false, NULL)
ON CONFLICT (capability) DO NOTHING;

-- Auto-sectionizer rules. Name keywords first (most specific = longest pattern),
-- then capability-based fallbacks for devices whose names say nothing useful.
INSERT INTO dsapp.panel_section_rules (section_slug, match_kind, pattern, priority) VALUES
    ('kitchen',  'name_keyword', 'kitchen',      10),
    ('kitchen',  'name_keyword', 'coffee',       10),
    ('kitchen',  'name_keyword', 'fridge',       10),
    ('living',   'name_keyword', 'living',       10),
    ('living',   'name_keyword', 'lounge',       10),
    ('living',   'name_keyword', 'tv',           20),
    ('dining',   'name_keyword', 'dining',       10),
    ('bedroom',  'name_keyword', 'bedroom',      10),
    ('bedroom',  'name_keyword', 'bed ',         20),
    ('office',   'name_keyword', 'office',       10),
    ('office',   'name_keyword', 'desk',         20),
    ('bathroom', 'name_keyword', 'bathroom',     10),
    ('bathroom', 'name_keyword', 'shower',       15),
    ('bathroom', 'name_keyword', 'toilet',       15),
    ('hallway',  'name_keyword', 'hallway',      10),
    ('hallway',  'name_keyword', 'stairs',       15),
    ('hallway',  'name_keyword', 'entry',        15),
    ('hallway',  'name_keyword', 'foyer',        15),
    ('laundry',  'name_keyword', 'laundry',      10),
    ('laundry',  'name_keyword', 'washer',       15),
    ('laundry',  'name_keyword', 'dryer',        15),
    ('basement', 'name_keyword', 'basement',     10),
    ('garage',   'name_keyword', 'garage',       10),
    ('garage',   'name_keyword', 'driveway',     20),
    ('outdoor',  'name_keyword', 'outdoor',      10),
    ('outdoor',  'name_keyword', 'outside',      10),
    ('outdoor',  'name_keyword', 'terrace',      10),
    ('outdoor',  'name_keyword', 'patio',        10),
    ('outdoor',  'name_keyword', 'yard',         10),
    ('outdoor',  'name_keyword', 'garden',       10),
    ('outdoor',  'name_keyword', 'pool',         10),
    ('outdoor',  'name_keyword', 'porch',        10),
    ('outdoor',  'name_keyword', 'deck',         15),
    -- Capability fallbacks: a device whose NAME reveals no room still lands
    -- somewhere sensible rather than in the 'other' bucket.
    ('climate',  'capability',   'Thermostat',   200),
    ('security', 'capability',   'Lock',         200),
    ('security', 'capability',   'SmokeDetector',210),
    ('security', 'capability',   'WaterSensor',  210)
ON CONFLICT DO NOTHING;

-- PostgREST cache reload (new tables/comments).
-- NOTE: these tables are DELIBERATELY NOT granted to smarthome_anon and get no
-- api.* view — the panel surface is reached only through the authenticated
-- FastAPI routes in apps/tiles_api/, never by a client talking to PostgREST.
NOTIFY pgrst, 'reload schema';
