use std::{env, fs, io, path::Path};

fn collect(root: &Path, directory: &Path, files: &mut Vec<String>) -> io::Result<()> {
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        if entry.file_name() == "__pycache__" {
            continue;
        }
        let kind = entry.file_type()?;
        let path = entry.path();
        if kind.is_dir() {
            collect(root, &path, files)?;
        } else if kind.is_file() {
            let name = path.strip_prefix(root).unwrap().to_str().unwrap();
            files.push(name.replace(std::path::MAIN_SEPARATOR, "/"));
        }
    }
    Ok(())
}

fn main() -> io::Result<()> {
    let root = Path::new("../skills/sai");
    println!("cargo:rerun-if-changed={}", root.display());
    let mut files = Vec::new();
    collect(root, root, &mut files)?;
    files.sort();
    let mut module = String::from("&[\n");
    for name in files {
        module.push_str(&format!(
            "({name:?}, include_bytes!(concat!(env!(\"CARGO_MANIFEST_DIR\"), \"/../skills/sai/\", {name:?}))),\n"
        ));
    }
    module.push_str("]\n");
    fs::write(
        Path::new(&env::var_os("OUT_DIR").unwrap()).join("skill.rs"),
        module,
    )
}
