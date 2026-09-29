//! Short-lived native drag/drop grants for custom card-art imports.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use getrandom::fill;
use serde_json::Value;

pub(crate) const MAX_PATHS: usize = 256;
pub(crate) const MAX_PATH_BYTES: usize = 4096;
const MAX_GRANTS: usize = 8;
const GRANT_TTL: Duration = Duration::from_secs(60);

/// Wry 0.55 reports Cocoa points on macOS, but physical pixels on other
/// platforms. DOM hit testing always uses logical viewport coordinates.
/// Keep this conversion at the native boundary, not in page heuristics.
pub(crate) fn logical_drop_position(
    x: f64,
    y: f64,
    scale: f64,
    cocoa_points: bool,
) -> Option<(f64, f64)> {
    if !x.is_finite() || !y.is_finite() || !scale.is_finite() || scale <= 0.0 {
        return None;
    }
    let divisor = if cocoa_points { 1.0 } else { scale };
    Some((x / divisor, y / divisor))
}

#[derive(Debug)]
struct Grant {
    window: String,
    paths: Vec<String>,
    expires: Instant,
}

#[derive(Default, Debug)]
pub(crate) struct DropGrants {
    grants: Mutex<HashMap<String, Grant>>,
}

impl DropGrants {
    pub(crate) fn issue(&self, window: &str, paths: &[PathBuf]) -> Result<String, String> {
        if window != "main" || paths.is_empty() || paths.len() > MAX_PATHS {
            return Err("invalid dropped file batch".into());
        }
        let paths = paths
            .iter()
            .map(|path| {
                let value = path
                    .to_str()
                    .ok_or_else(|| "dropped path is not valid UTF-8".to_string())?;
                validate_path(value)?;
                if !PathBuf::from(value).is_absolute() {
                    return Err("dropped path is not absolute".into());
                }
                Ok(value.to_owned())
            })
            .collect::<Result<Vec<_>, String>>()?;
        let mut grants = self
            .grants
            .lock()
            .map_err(|_| "drop grants unavailable".to_string())?;
        grants.retain(|_, grant| grant.expires > Instant::now());
        if grants.len() >= MAX_GRANTS {
            return Err("too many pending file drops".into());
        }
        let mut bytes = [0_u8; 16];
        fill(&mut bytes).map_err(|_| "could not create drop grant".to_string())?;
        let token = bytes
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        grants.insert(
            token.clone(),
            Grant {
                window: window.to_owned(),
                paths,
                expires: Instant::now() + GRANT_TTL,
            },
        );
        Ok(token)
    }

    pub(crate) fn consume(&self, window: &str, token: &str) -> Result<Vec<String>, String> {
        if window != "main" || !valid_token(token) {
            return Err("invalid or expired drop grant".into());
        }
        let mut grants = self
            .grants
            .lock()
            .map_err(|_| "drop grants unavailable".to_string())?;
        grants.retain(|_, grant| grant.expires > Instant::now());
        let grant = grants
            .remove(token)
            .ok_or_else(|| "invalid or expired drop grant".to_string())?;
        if grant.window != window {
            return Err("invalid or expired drop grant".into());
        }
        Ok(grant.paths)
    }

    #[cfg(test)]
    fn insert_for_test(&self, token: &str, window: &str, expires: Instant) {
        self.grants.lock().unwrap().insert(
            token.to_string(),
            Grant {
                window: window.to_string(),
                paths: vec!["/tmp/a.png".to_string()],
                expires,
            },
        );
    }
}

pub(crate) fn validate_path(path: &str) -> Result<(), String> {
    if path.is_empty()
        || path.len() > MAX_PATH_BYTES
        || path
            .chars()
            .any(|character| character.is_control() || character == '\u{7f}')
    {
        return Err("invalid dropped path".into());
    }
    Ok(())
}

pub(crate) fn valid_token(token: &str) -> bool {
    token.len() == 32
        && token
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

pub(crate) fn validate_destination(destination: &str) -> bool {
    matches!(destination, "front" | "double_sided" | "back")
}

pub(crate) fn validate_open_folder(params: &Value) -> Result<(), String> {
    let object = params
        .as_object()
        .ok_or_else(|| "invalid custom art parameters".to_string())?;
    if object.len() != 1
        || object
            .get("destination")
            .and_then(Value::as_str)
            .is_none_or(|destination| !validate_destination(destination))
    {
        return Err("invalid custom art parameters".into());
    }
    Ok(())
}

pub(crate) fn valid_open_folder_result(value: &Value) -> bool {
    let Some(object) = value.as_object() else {
        return false;
    };
    let Some(errors) = object.get("errors").and_then(Value::as_array) else {
        return false;
    };
    if object.len() != 2
        || errors.len() > 8
        || !errors
            .iter()
            .all(|error| error.as_str().is_some_and(|text| text.len() <= 256))
    {
        return false;
    }
    match object.get("ok").and_then(Value::as_bool) {
        Some(true) => errors.is_empty(),
        Some(false) => !errors.is_empty(),
        None => false,
    }
}

pub(crate) fn valid_terminal_result(value: &Value) -> bool {
    let Some(object) = value.as_object() else {
        return false;
    };
    if object.get("ok").and_then(Value::as_bool) == Some(false) {
        return object.len() == 2
            && object
                .get("errors")
                .and_then(Value::as_array)
                .is_some_and(|errors| {
                    !errors.is_empty()
                        && errors.len() <= 8
                        && errors.iter().all(|error| {
                            error
                                .as_str()
                                .is_some_and(|text| !text.is_empty() && text.len() <= 256)
                        })
                });
    }
    let limit = if object.get("destination").and_then(Value::as_str) == Some("back") {
        1
    } else {
        MAX_PATHS
    };
    object.len() == 5
        && object.get("ok").and_then(Value::as_bool) == Some(true)
        && object
            .get("destination")
            .and_then(Value::as_str)
            .is_some_and(validate_destination)
        && object
            .get("imported")
            .and_then(Value::as_u64)
            .is_some_and(|n| n <= limit as u64)
        && object
            .get("names")
            .and_then(Value::as_array)
            .is_some_and(|names| {
                names.len() <= limit
                    && object.get("imported").and_then(Value::as_u64) == Some(names.len() as u64)
                    && names.iter().all(|name| safe_basename(name))
            })
        && object
            .get("failed")
            .and_then(Value::as_array)
            .is_some_and(|failed| {
                failed.len()
                    + object
                        .get("names")
                        .and_then(Value::as_array)
                        .map_or(0, Vec::len)
                    <= limit
                    && failed.iter().all(|entry| {
                        let Some(entry) = entry.as_object() else {
                            return false;
                        };
                        entry.len() == 2
                            && entry.get("name").is_some_and(safe_basename)
                            && entry
                                .get("error")
                                .and_then(Value::as_str)
                                .is_some_and(|text| !text.is_empty() && text.len() <= 512)
                    })
            })
}

fn safe_basename(value: &Value) -> bool {
    value.as_str().is_some_and(|name| {
        !name.is_empty()
            && name.len() <= 255
            && name != "."
            && name != ".."
            && !name.contains(['/', '\\', ':'])
            && !name
                .chars()
                .any(|character| character.is_control() || character == '\u{7f}')
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn drop_coordinates_preserve_cocoa_points_and_scale_physical_pixels() {
        for scale in [1.0, 1.5, 2.0, 3.0] {
            // The same drop in the front zone must stay there on Retina.
            assert_eq!(
                logical_drop_position(720.0, 540.0, scale, true),
                Some((720.0, 540.0))
            );
            assert_eq!(
                logical_drop_position(720.0 * scale, 540.0 * scale, scale, false),
                Some((720.0, 540.0))
            );
        }
        for scale in [0.0, -1.0, f64::NAN, f64::INFINITY] {
            assert_eq!(logical_drop_position(720.0, 540.0, scale, false), None);
        }
        assert_eq!(logical_drop_position(f64::NAN, 540.0, 2.0, true), None);
        assert_eq!(logical_drop_position(720.0, f64::INFINITY, 2.0, true), None);
    }

    #[test]
    fn grants_are_random_one_use_and_bound_to_main() {
        let grants = DropGrants::default();
        let path = std::env::temp_dir().join("front.png");
        let expected = path.to_str().unwrap().to_owned();
        let token = grants.issue("main", &[path]).unwrap();
        assert!(valid_token(&token));
        assert_eq!(
            grants.consume("other", &token),
            Err("invalid or expired drop grant".into())
        );
        assert_eq!(grants.consume("main", &token).unwrap(), vec![expected]);
        assert!(grants.consume("main", &token).is_err());
    }

    #[test]
    fn grants_expire_and_bound_path_inputs() {
        let grants = DropGrants::default();
        let token = "a".repeat(32);
        grants.insert_for_test(&token, "main", Instant::now() - Duration::from_secs(1));
        assert!(grants.consume("main", &token).is_err());
        assert!(validate_path(&format!("/{}", "x".repeat(MAX_PATH_BYTES))).is_err());
        assert!(validate_path("/tmp/a\n.png").is_err());
        assert!(grants
            .issue("main", &vec![PathBuf::from("/tmp/a"); MAX_PATHS + 1])
            .is_err());
    }

    #[test]
    fn validates_fixed_destinations_open_shape_and_terminal_limits() {
        assert!(validate_destination("front"));
        assert!(validate_destination("back"));
        assert!(validate_open_folder(&json!({"destination":"back"})).is_ok());
        assert!(valid_terminal_result(
            &json!({"ok":true,"destination":"back","imported":1,"names":["new.png"],"failed":[]})
        ));
        assert!(!valid_terminal_result(
            &json!({"ok":true,"destination":"back","imported":2,"names":["a.png","b.png"],"failed":[]})
        ));
        let combined = json!({"ok":true,"destination":"front","imported":MAX_PATHS,"names":vec!["x.png";MAX_PATHS],"failed":[{"name":"bad.png","error":"bad"}]});
        assert!(!valid_terminal_result(&combined));
        assert!(!validate_destination("/tmp/front"));
        assert!(validate_open_folder(&json!({"destination":"front"})).is_ok());
        assert!(validate_open_folder(&json!({"destination":"front", "path":"/tmp"})).is_err());
        let result =
            json!({"ok":true,"destination":"front","imported":1,"names":["x.png"],"failed":[]});
        assert!(valid_terminal_result(&result));
        let inconsistent =
            json!({"ok":true,"destination":"front","imported":2,"names":["x.png"],"failed":[]});
        assert!(!valid_terminal_result(&inconsistent));
        let failures = (0..=MAX_PATHS)
            .map(|_| json!({"name":"x.png","error":"bad"}))
            .collect::<Vec<_>>();
        let too_many =
            json!({"ok":true,"destination":"front","imported":0,"names":[],"failed":failures});
        assert!(!valid_terminal_result(&too_many));
    }
}
