// Kabala: types for the vendored mustache.mjs (the project's types live in a separate @types package)
type Token = [string, string, number, number, ...unknown[]];
interface Mustache {
  parse(template: string, tags?: [string, string]): Token[];
  render(template: string, view: object, partials?: object, config?: { escape?: (value: unknown) => string; tags?: [string, string] }): string;
  escape(value: string): string;
  clearCache(): void;
  templateCache: { _keys: string[] } | undefined;
}
declare const mustache: Mustache;
export default mustache;
