// Kabala: types for the vendored isEmail (validator.js ships its types in a separate @types package)
export interface IsEmailOptions {
  allow_display_name?: boolean;
  require_display_name?: boolean;
  allow_utf8_local_part?: boolean;
  require_tld?: boolean;
  ignore_max_length?: boolean;
  allow_ip_domain?: boolean;
  domain_specific_validation?: boolean;
  blacklisted_chars?: string;
  host_blacklist?: (string | RegExp)[];
  host_whitelist?: (string | RegExp)[];
}
export default function isEmail(str: string, options?: IsEmailOptions): boolean;
