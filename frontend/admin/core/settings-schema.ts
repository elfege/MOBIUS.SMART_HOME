/**
 * admin/core/settings-schema.ts — PURE derivation of an editable field list
 * from an app-type's `settings_schema` (JSON-Schema-shaped) plus an instance's
 * current `settings` values. No React imports — unit-testable logic.
 *
 * Field kinds map 1:1 to the field components in components/fields/:
 *   boolean          -> BooleanField (switch)
 *   enum             -> EnumField (chip row; any type with an `enum` list)
 *   integer | number -> NumberField (numeric input, min/max validated)
 *   string           -> StringField (text input)
 *   opaque           -> OpaqueField (read-only JSON — arrays/objects/unknown;
 *                       HONEST: shown, labeled read-only, never silently hidden)
 */

export type FieldKind =
  | 'boolean'
  | 'enum'
  | 'integer'
  | 'number'
  | 'string'
  | 'opaque';

/** One editable (or read-only) setting derived from the schema. */
export interface FieldSpec {
  key: string;
  title: string;
  kind: FieldKind;
  /** Current effective value: instance setting, else schema default, else null. */
  value: unknown;
  defaultValue: unknown;
  enumOptions: string[] | null;
  minimum: number | null;
  maximum: number | null;
}

/** The subset of JSON Schema this backend actually emits per property. */
interface SchemaProp {
  type?: string;
  title?: string;
  default?: unknown;
  enum?: unknown[];
  minimum?: number;
  maximum?: number;
}

function kindOf(prop: SchemaProp): FieldKind {
  if (Array.isArray(prop.enum) && prop.enum.length > 0) return 'enum';
  switch (prop.type) {
    case 'boolean':
      return 'boolean';
    case 'integer':
      return 'integer';
    case 'number':
      return 'number';
    case 'string':
      return 'string';
    default:
      return 'opaque'; // array / object / missing type — read-only JSON
  }
}

/** Derive the ordered field list. Schema property order is preserved (it is
 *  the order the app author declared and the jQuery UI renders). */
export function deriveFields(
  settingsSchema: Record<string, unknown>,
  settings: Record<string, unknown>,
): FieldSpec[] {
  const props = settingsSchema['properties'];
  if (!props || typeof props !== 'object') return [];

  const out: FieldSpec[] = [];
  for (const [key, raw] of Object.entries(props as Record<string, unknown>)) {
    if (!raw || typeof raw !== 'object') continue;
    const prop = raw as SchemaProp;
    const kind = kindOf(prop);
    out.push({
      key,
      title: prop.title ?? key,
      kind,
      value: key in settings ? settings[key] : prop.default ?? null,
      defaultValue: prop.default ?? null,
      enumOptions:
        kind === 'enum' && Array.isArray(prop.enum)
          ? prop.enum.map((e) => String(e))
          : null,
      minimum: typeof prop.minimum === 'number' ? prop.minimum : null,
      maximum: typeof prop.maximum === 'number' ? prop.maximum : null,
    });
  }
  return out;
}

/** Validate one numeric field edit. Returns an error message or null. */
export function validateNumber(spec: FieldSpec, text: string): string | null {
  if (text.trim() === '') return 'required';
  const n = Number(text);
  if (!Number.isFinite(n)) return 'not a number';
  if (spec.kind === 'integer' && !Number.isInteger(n)) return 'must be a whole number';
  if (spec.minimum !== null && n < spec.minimum) return `min ${spec.minimum}`;
  if (spec.maximum !== null && n > spec.maximum) return `max ${spec.maximum}`;
  return null;
}
