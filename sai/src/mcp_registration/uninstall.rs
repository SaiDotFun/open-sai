use std::time::Instant;

use clap::Args;
use sai_core::errors::SaiError;
use serde_json::json;

use super::{
    CLIENT_TIMEOUT, CLIENTS, CODEX_PROJECT_ENTRY, CODEX_STILL_ACTIVE, Client, ClientResult,
    Clients, NAMES, Registration, RegistrationResult, Scope, Target, chosen, client, finish,
    interactive, pick_clients, server_target,
};

#[derive(Args)]
pub(crate) struct UninstallArgs {
    clients: Vec<String>,
    /// Remove Sai from every client.
    #[arg(long = "all")]
    all_clients: bool,
    /// Show the changes without writing them.
    #[arg(long)]
    dry_run: bool,
    /// Remove only this scope. The default covers every registered scope.
    #[arg(long, value_enum)]
    scope: Option<Scope>,
}

impl UninstallArgs {
    pub(super) fn run(self, clients: &Clients, json_output: bool) -> Result<(), SaiError> {
        let mut names = chosen(self.clients)?;
        let target = server_target(Vec::new(), &clients.home).ok();
        if self.all_clients {
            names = CLIENTS.to_vec();
        } else if names.is_empty() {
            names = clients.pick_uninstall(target.as_ref())?;
        }
        let results = names
            .into_iter()
            .flat_map(|client| clients.uninstall(client, self.scope, self.dry_run, target.as_ref()))
            .collect();
        finish(results, json_output)
    }
}

impl Clients {
    fn pick_uninstall(&self, target: Option<&Target>) -> Result<Vec<Client>, SaiError> {
        let rows = CLIENTS.map(|client| (client, self.list_client(client, target)));
        let registered = rows.iter().filter(|(_, row)| row["registered"] == true);
        if interactive() {
            let choices = registered.map(|(client, _)| *client).collect::<Vec<_>>();
            if choices.is_empty() {
                return Err(SaiError::usage("Sai is not registered with any client."));
            }
            return pick_clients("Remove Sai from (numbers, names, or all)", choices);
        }
        let where_registered = registered
            .map(|(client, row)| {
                let scopes = row["registrations"]
                    .as_array()
                    .expect("list_client reports registrations")
                    .iter()
                    .filter_map(|entry| entry["scope"].as_str())
                    .collect::<Vec<_>>()
                    .join(", ");
                format!("{} ({scopes})", client.name())
            })
            .collect::<Vec<_>>()
            .join(", ");
        let where_registered = if where_registered.is_empty() {
            "no client"
        } else {
            &where_registered
        };
        Err(SaiError::usage(format!(
            "Name clients to remove, or pass --all. Sai is registered with: {where_registered}."
        )))
    }

    fn uninstall(
        &self,
        client: Client,
        scope: Option<Scope>,
        dry: bool,
        target: Option<&Target>,
    ) -> Vec<RegistrationResult> {
        let deadline = Instant::now() + CLIENT_TIMEOUT;
        let found = match self.all(client, deadline) {
            Ok(found) => found,
            Err(error) => {
                let mut result = RegistrationResult::new(client, scope);
                result.failed(error);
                return vec![result];
            }
        };
        let mut results = Vec::new();
        for (name, entries) in &found {
            let mut selected = entries
                .iter()
                .filter(|entry| scope.is_none_or(|scope| entry.scope == scope))
                .collect::<Vec<_>>();
            if client == Client::Codex
                && scope.is_some_and(|scope| scope != Scope::Project)
                && let Some(project) = entries
                    .first()
                    .filter(|entry| entry.scope == Scope::Project)
            {
                selected.push(project);
            }
            for entry in selected {
                let mut result = RegistrationResult::new(client, entry.scope);
                if *name != NAMES[0] {
                    result.report["name"] = json!(name);
                }
                let shown = entry.report(target);
                result.report["command"] = shown["command"].clone();
                if let Some(args) = shown.get("args") {
                    result.report["args"] = args.clone();
                }
                if let Err(error) =
                    self.uninstall_one(client, name, entry, dry, &mut result, deadline)
                {
                    result.failed(error);
                }
                results.push(result);
            }
        }
        if results.is_empty() {
            let mut result = RegistrationResult::new(client, scope);
            result.report["action"] = json!("absent");
            results.push(result);
        }
        results
    }

    fn uninstall_one(
        &self,
        client: Client,
        name: &str,
        entry: &Registration,
        dry: bool,
        result: &mut RegistrationResult,
        deadline: Instant,
    ) -> ClientResult<()> {
        let scope = entry.scope;
        if client == Client::Codex && scope == Scope::Project {
            return Err(CODEX_PROJECT_ENTRY.into());
        }
        let path = client.path(scope, &self.home, &self.cwd);
        let step = if client == Client::Claude {
            let executable = client.executable().ok_or("`claude` is not on PATH")?;
            let args = [
                executable.to_string_lossy().into_owned(),
                "mcp".into(),
                "remove".into(),
                "--scope".into(),
                scope.name().into(),
                name.into(),
            ];
            result.report["argv"] = json!([args]);
            Some(args)
        } else {
            result.report["path"] = json!(path);
            None
        };
        if dry {
            result.planned("removed");
            return Ok(());
        }
        if let Some(step) = step {
            client::check_writable(&path)?;
            client::run_step(&step, &self.cwd, deadline)?;
            result.changed = true;
        } else {
            result.changed = client.remove(&path, name, entry, deadline)?;
        }
        self.verify_removed(client, name, scope, deadline)?;
        result.report["action"] = json!("removed");
        Ok(())
    }

    fn verify_removed(
        &self,
        client: Client,
        name: &str,
        scope: Scope,
        deadline: Instant,
    ) -> ClientResult<()> {
        if self
            .registrations(client, name, deadline)?
            .iter()
            .any(|entry| entry.scope == scope)
        {
            return Err(format!("{} still shows the sai entry", client.name()));
        }
        if client == Client::Codex
            && client::codex_get(&self.cwd, Scope::Project, name, deadline)?.is_some()
        {
            return Err(CODEX_STILL_ACTIVE.into());
        }
        Ok(())
    }
}
