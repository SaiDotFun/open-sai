use std::{fs, path::Path, sync::Barrier, thread};

use sai_core::settings::{self, Environment, Flags, KEYS, Key, Settings, Source};
use serde_json::json;
use tempfile::tempdir;

fn fixture(path: &Path, vars: &[(&str, &str)]) -> Environment {
    Environment::new(path.into(), |name| {
        vars.iter()
            .find(|(key, _)| *key == name)
            .map(|(_, value)| (*value).into())
    })
}

#[test]
fn resolves_all_layers_and_uses_websocket_from_the_same_or_higher_layer() {
    let home = tempdir().unwrap();
    let env = fixture(
        home.path(),
        &[
            ("SAI_ENDPOINT", " https://env.example/query "),
            ("SAI_WS_ENDPOINT", "wss://other.example/ws"),
            ("SAI_LOG_KEEP_DAYS", " \t"),
        ],
    );
    fs::write(env.path(), "keeper.endpoint = \"https://file.example/query\"\n[lcd]\nendpoint = \"http://127.0.0.1:8000\"\n").unwrap();

    let from_env = Settings::load(&Flags::default(), &env).unwrap();
    assert_eq!(from_env.endpoint(), "https://env.example/query");
    assert_eq!(from_env.source(Key::KeeperEndpoint), Source::Env);
    assert_eq!(from_env.ws_endpoint(), "wss://other.example/ws");
    assert_eq!(from_env.source(Key::KeeperWsEndpoint), Source::Env);
    assert_eq!(from_env.lcd_endpoint(), "http://127.0.0.1:8000");
    assert_eq!(from_env.source(Key::LcdEndpoint), Source::File);
    assert_eq!(from_env.source(Key::LogsKeepDays), Source::Default);
    let flags = Flags {
        endpoint: Some("HTTP://user:pw@localhost:42/a/../b?key=secret#fragment".into()),
        ..Flags::default()
    };
    let from_flag = Settings::load(&flags, &env).unwrap();
    assert_eq!(from_flag.source(Key::KeeperEndpoint), Source::Flag);
    assert_eq!(
        from_flag.ws_endpoint(),
        "ws://user:pw@localhost:42/a/../b?key=secret#fragment"
    );
    assert_eq!(from_flag.source(Key::KeeperWsEndpoint), Source::Derived);
}

#[test]
fn file_type_errors_are_reported_before_overrides_except_for_logging() {
    let home = tempdir().unwrap();
    let env = fixture(
        home.path(),
        &[
            ("SAI_ENDPOINT", "https://env.example"),
            ("SAI_LOG_KEEP_DAYS", "wrong"),
        ],
    );
    fs::write(env.path(), "[keeper]\nendpoint = 42\n").unwrap();
    let error = Settings::load(&Flags::default(), &env).unwrap_err();
    assert_eq!(
        error.to_string(),
        format!(
            "{}: Invalid value for keeper.endpoint: expected a URL string",
            env.path().display()
        )
    );

    fs::write(env.path(), "logs = false\ntheme = \"dark\"\n").unwrap();
    let resolved = Settings::load(&Flags::default(), &env).unwrap();
    assert_eq!(resolved.value(Key::LogsEnabled), &json!(false));
    assert_eq!(resolved.source(Key::LogsEnabled), Source::Invalid);
    assert_eq!(resolved.source(Key::LogsKeepDays), Source::Invalid);
    assert_eq!(
        resolved.warnings,
        [
            format!(
                "{}: 'logs' must be a table, such as [logs]; logging is off until it is fixed",
                env.path().display()
            ),
            format!("{}: ignoring unknown setting 'theme'", env.path().display()),
            "SAI_LOG_KEEP_DAYS: invalid value for logs.keep_days; logging is off until it is fixed"
                .to_owned(),
        ]
    );
}

#[test]
fn disabled_logging_ignores_bad_retention_from_env() {
    let home = tempdir().unwrap();
    let env = fixture(
        home.path(),
        &[("SAI_LOG", "No"), ("SAI_LOG_KEEP_DAYS", "wrong")],
    );
    let resolved = Settings::load(&Flags::default(), &env).unwrap();
    assert_eq!(resolved.value(Key::LogsEnabled), &json!(false));
    assert_eq!(resolved.value(Key::LogsKeepDays), &json!(14));
    assert_eq!(resolved.source(Key::LogsKeepDays), Source::Env);
    assert!(resolved.warnings.is_empty());
}

#[test]
fn new_config_round_trips_all_placeholders() {
    let home = tempdir().unwrap();
    let config = home.path().join("config.toml");
    let template = include_str!("../src/settings-template.toml");
    for key in KEYS {
        let raw = match key {
            Key::KeeperEndpoint | Key::LcdEndpoint => "https://staging.example",
            Key::KeeperWsEndpoint => "wss://staging.example",
            Key::UpdateAuto | Key::LogsEnabled => "no",
            Key::LogsKeepDays => "4",
        };
        settings::set(&config, key, &key.parse_value(raw).unwrap()).unwrap();
        let env = fixture(home.path(), &[]);
        let resolved = Settings::load(&Flags::default(), &env).unwrap();
        assert_eq!(resolved.value(key), &key.parse_value(raw).unwrap());
        assert!(settings::unset(&config, key).unwrap());
        assert_eq!(fs::read_to_string(&config).unwrap(), template);
        assert!(!settings::unset(&config, key).unwrap());
    }
}

#[test]
fn edits_keep_formatting_and_annotations_and_do_not_touch_a_no_op() {
    let home = tempdir().unwrap();
    let config = home.path().join("config.toml");
    let original = "# top\n[keeper]\n# our Keeper\n\"endpoint\" = 'https://old.example/q'  # pinned\n# bottom\n\n[update]\nauto=true\n";
    fs::write(&config, original).unwrap();
    let key = Key::KeeperEndpoint;
    settings::set(
        &config,
        key,
        &key.parse_value("https://new.example/q").unwrap(),
    )
    .unwrap();
    assert_eq!(
        fs::read_to_string(&config).unwrap(),
        original.replace("'https://old.example/q'", "\"https://new.example/q\"")
    );
    assert!(settings::unset(&config, key).unwrap());
    let unset = fs::read_to_string(&config).unwrap();
    assert!(
        unset.contains("# endpoint = \"https://sai-keeper.nibiru.fi/query\"  # pinned\n# bottom")
    );
    let previous = fs::metadata(&config).unwrap().modified().unwrap();
    assert!(!settings::unset(&config, key).unwrap());
    assert_eq!(fs::metadata(&config).unwrap().modified().unwrap(), previous);

    settings::set(
        &config,
        key,
        &key.parse_value("https://another.example/q").unwrap(),
    )
    .unwrap();
    assert!(
        fs::read_to_string(&config)
            .unwrap()
            .contains("endpoint = \"https://another.example/q\"  # pinned\n# bottom")
    );
}

#[test]
fn placeholder_inside_multiline_value_is_left_alone() {
    let home = tempdir().unwrap();
    let config = home.path().join("config.toml");
    let original = "[keeper]\nnotes = '''\n# endpoint = secret\n'''\n";
    fs::write(&config, original).unwrap();
    let key = Key::KeeperEndpoint;
    settings::set(
        &config,
        key,
        &key.parse_value("https://new.example").unwrap(),
    )
    .unwrap();
    assert_eq!(
        fs::read_to_string(&config).unwrap(),
        format!("{original}endpoint = \"https://new.example\"\n")
    );
}

#[test]
fn reads_nonstandard_toml_sections_but_refuses_to_rewrite_them() {
    let home = tempdir().unwrap();
    let env = fixture(home.path(), &[]);
    let path = env.path();
    let key = Key::KeeperEndpoint;
    for input in [
        "keeper = { endpoint = \"https://file.example/q\" } # inline\n",
        "keeper.endpoint = \"https://file.example/q\" # dotted\n",
        "[keeper.unknown]\nendpoint = \"https://file.example/q\"\n",
    ] {
        fs::write(&path, input).unwrap();
        assert!(Settings::load(&Flags::default(), &env).is_ok());
        assert!(
            settings::set(&path, key, &key.parse_value("https://new.example").unwrap())
                .unwrap_err()
                .is_uneditable()
        );
        assert!(settings::unset(&path, key).unwrap_err().is_uneditable());
        assert_eq!(fs::read_to_string(&path).unwrap(), input);
    }
}

#[cfg(unix)]
#[test]
fn concurrent_edits_keep_changes_and_private_modes() {
    use std::os::unix::fs::PermissionsExt;

    let home = tempdir().unwrap();
    let env = fixture(&home.path().join("new-home"), &[]);
    let path = env.path();
    let keys = [
        (Key::KeeperEndpoint, "https://one.example"),
        (Key::LcdEndpoint, "https://two.example"),
        (Key::UpdateAuto, "false"),
        (Key::LogsKeepDays, "5"),
    ];
    let ready = Barrier::new(keys.len());
    thread::scope(|scope| {
        for (key, raw) in keys {
            let path = &path;
            let ready = &ready;
            scope.spawn(move || {
                ready.wait();
                settings::set(path, key, &key.parse_value(raw).unwrap()).unwrap();
            });
        }
    });
    let resolved = Settings::load(&Flags::default(), &env).unwrap();
    for (key, raw) in keys {
        assert_eq!(resolved.value(key), &key.parse_value(raw).unwrap());
    }
    let mode = |path: &Path| fs::metadata(path).unwrap().permissions().mode() & 0o777;
    assert_eq!(mode(&env.home), 0o700);
    assert_eq!(mode(&path), 0o600);
    assert_eq!(mode(&env.home.join(".config.lock")), 0o600);
}

#[test]
fn oversized_retention_values_keep_json_precision_and_can_be_edited() {
    let home = tempdir().unwrap();
    let env = fixture(home.path(), &[]);
    let path = env.path();
    let huge = "184467440737095516160001234";
    let initial = format!("[logs]\nkeep_days = {huge}  # still keep this note\n");
    fs::write(&path, &initial).unwrap();
    let resolved = Settings::load(&Flags::default(), &env).unwrap();
    assert_eq!(resolved.value(Key::LogsKeepDays).to_string(), huge);
    assert_eq!(resolved.source(Key::LogsKeepDays), Source::File);

    settings::set(&path, Key::LogsKeepDays, &json!(2)).unwrap();
    assert_eq!(
        fs::read_to_string(&path).unwrap(),
        initial.replace(huge, "2")
    );

    fs::write(&path, "[logs]\nkeep_days = 0xFFFF_FFFF_FFFF_FFFF\n").unwrap();
    let resolved = Settings::load(&Flags::default(), &env).unwrap();
    assert_eq!(resolved.value(Key::LogsKeepDays).as_u64(), Some(u64::MAX));
    assert!(settings::unset(&path, Key::LogsKeepDays).unwrap());
    assert_eq!(
        fs::read_to_string(&path).unwrap(),
        "[logs]\n# keep_days = 14\n"
    );
}

#[test]
fn adding_to_an_explicit_table_keeps_its_subtable_separate() {
    let home = tempdir().unwrap();
    let path = home.path().join("config.toml");
    for gap in ["", "\n"] {
        let input = format!("[keeper]\n# note\n{gap}[keeper.future]\nfield = 'unchanged'\n");
        fs::write(&path, input).unwrap();
        settings::set(&path, Key::KeeperEndpoint, &json!("https://new.example")).unwrap();
        assert_eq!(
            fs::read_to_string(&path).unwrap(),
            "[keeper]\n# note\nendpoint = \"https://new.example\"\n\n[keeper.future]\nfield = 'unchanged'\n"
        );
    }
}

#[test]
fn repairs_dotted_assignments_inside_an_explicit_section() {
    let home = tempdir().unwrap();
    let path = home.path().join("config.toml");
    let new = json!("https://new.example");
    let key = Key::KeeperEndpoint;

    fs::write(&path, "[keeper]\nendpoint.a = 1").unwrap();
    settings::set(&path, key, &new).unwrap();
    assert_eq!(
        fs::read_to_string(&path).unwrap(),
        "[keeper]\nendpoint = \"https://new.example\"\n",
    );
    fs::write(&path, "[keeper]\nendpoint.a = 1").unwrap();
    assert!(settings::unset(&path, key).unwrap());
    assert_eq!(
        fs::read_to_string(&path).unwrap(),
        "[keeper]\n# endpoint = \"https://sai-keeper.nibiru.fi/query\"\n",
    );

    let input = "[keeper]\nendpoint.a = 1\nws_endpoint = \"wss://old.example\"\nendpoint.b = [\n  2,\n]\n# lcd note\n[lcd]\nendpoint = \"https://lcd.example\"\n";
    fs::write(&path, input).unwrap();
    settings::set(&path, key, &new).unwrap();
    assert_eq!(
        fs::read_to_string(&path).unwrap(),
        "[keeper]\nws_endpoint = \"wss://old.example\"\n# lcd note\nendpoint = \"https://new.example\"\n[lcd]\nendpoint = \"https://lcd.example\"\n",
    );
    fs::write(&path, input).unwrap();
    assert!(settings::unset(&path, key).unwrap());
    assert_eq!(
        fs::read_to_string(&path).unwrap(),
        "[keeper]\n# endpoint = \"https://sai-keeper.nibiru.fi/query\"\nws_endpoint = \"wss://old.example\"\n# lcd note\n[lcd]\nendpoint = \"https://lcd.example\"\n",
    );
}

#[test]
fn editing_any_registry_key_preserves_other_tables_byte_for_byte() {
    let home = tempdir().unwrap();
    let path = home.path().join("config.toml");
    let before = "[future.first]\n# first\nlist = [\n  1,\n]\n\n";
    let between = "[future.middle]\n# middle café\nother.key = { keep = true }\n\n";
    let after = "[[future.last]] # last\nother = '''\n# keep verbatim\n'''\n";
    for key in KEYS {
        let (section, name) = key.spec().name.split_once('.').unwrap();
        let raw = match key {
            Key::KeeperEndpoint | Key::LcdEndpoint => "https://new.example",
            Key::KeeperWsEndpoint => "wss://new.example",
            Key::UpdateAuto | Key::LogsEnabled => "false",
            Key::LogsKeepDays => "7",
        };
        let value = key.parse_value(raw).unwrap();
        for shape in [
            String::new(),
            format!("# {name} = false\n"),
            format!("{name} = 2  # keep note\n"),
            format!("{name} = {{ nested = 2 }}\n"),
            format!("{name}.a.b = 1\nother = true\n\"{name}\".c = [\n  2,\n]\n"),
            format!("[{section}.{name}] # invalid setting\na = 1\n"),
            format!("[[{section}.{name}]]\na = 1\n"),
        ] {
            for newline in ["\n", "\r\n"] {
                let nested = format!("[{section}.{name}.tail]\nx = 1\n");
                let nested = if shape.starts_with('[') {
                    nested.as_str()
                } else {
                    ""
                };
                let input = format!("{before}[{section}]\n{shape}{between}{nested}{after}")
                    .replace('\n', newline);
                for unset in [false, true] {
                    fs::write(&path, &input).unwrap();
                    if unset {
                        settings::unset(&path, key).unwrap();
                    } else {
                        settings::set(&path, key, &value).unwrap();
                    }
                    let actual = fs::read_to_string(&path).unwrap();
                    assert!(
                        actual.starts_with(&before.replace('\n', newline)),
                        "{input:?} => {actual:?}"
                    );
                    assert!(
                        actual.contains(&between.replace('\n', newline)),
                        "{input:?} => {actual:?}"
                    );
                    assert!(
                        actual.ends_with(&after.replace('\n', newline)),
                        "{input:?} => {actual:?}"
                    );
                }
            }
        }
    }
}
