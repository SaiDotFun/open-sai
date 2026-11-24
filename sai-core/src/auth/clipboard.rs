use std::process::Stdio;

use tokio::process::Command;
use zeroize::Zeroizing;

use super::{installed, run_success};

pub const CLIPBOARD_NAMES: &str = "pbpaste, wl-paste, xclip, powershell.exe";

pub struct Clipboard {
    read: &'static [&'static str],
    clear: &'static [&'static str],
}

const TOOLS: &[Clipboard] = &[
    Clipboard {
        read: &["pbpaste"],
        clear: &["pbcopy"],
    },
    Clipboard {
        read: &["wl-paste", "--no-newline"],
        clear: &["wl-copy", "--clear"],
    },
    Clipboard {
        read: &["xclip", "-selection", "clipboard", "-o"],
        clear: &["xclip", "-selection", "clipboard"],
    },
    Clipboard {
        read: &[
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-Clipboard -Raw",
        ],
        clear: &[
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "Set-Clipboard -Value $null",
        ],
    },
];

impl Clipboard {
    pub async fn read() -> Option<(Zeroizing<String>, &'static Self)> {
        for tool in TOOLS.iter().filter(|tool| installed(tool.read[0])) {
            let mut command = Command::new(tool.read[0]);
            command
                .args(&tool.read[1..])
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());
            if let Ok(output) = run_success(&mut command, 10).await {
                let bytes = Zeroizing::new(output.stdout);
                if let Ok(text) =
                    std::str::from_utf8(bytes.strip_prefix(b"\xef\xbb\xbf").unwrap_or(&bytes))
                {
                    return Some((Zeroizing::new(text.trim().to_owned()), tool));
                }
            }
        }
        None
    }

    pub async fn clear(&self) -> bool {
        let mut command = Command::new(self.clear[0]);
        command
            .args(&self.clear[1..])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        run_success(&mut command, 10).await.is_ok()
    }
}
