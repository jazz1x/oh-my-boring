//! Stamps the binary with the commit it was built from.
//!
//! The engine already answers this on `/health`, but it gets the value from an env var the image
//! build passes in — so only the container knows what it is. The host CLI had no answer at all,
//! and `scripts/schedule-maintenance.sh` calls it daily: a stale host binary runs old logic
//! against live data, and the deploy-drift gate never sees it because that gate asks the engine.
//!
//! The trap this file has to avoid is becoming the stale thing itself. Cargo caches a build
//! script's output until one of its declared inputs changes, so a stamp resolved once at first
//! compile would go on reporting that commit forever — a version surface that lies is worse than
//! none. The `rerun-if` lines below are the whole defence: `.git/HEAD` covers checkouts and
//! commits on a branch, and the ref file it points at covers commits that move the branch under
//! us. Both are declared even when absent, because Cargo treats a missing declared path as a
//! reason to re-run rather than a reason to cache.

use std::path::{Path, PathBuf};
use std::process::Command;

fn main() {
    println!("cargo:rerun-if-env-changed=BUILD_SHA");
    println!("cargo:rerun-if-env-changed=BORING_BUILD_SHA");

    let git_dir = Path::new("..").join(".git");
    let head = git_dir.join("HEAD");
    println!("cargo:rerun-if-changed={}", head.display());
    if let Some(reference) = head_ref(&head) {
        println!(
            "cargo:rerun-if-changed={}",
            git_dir.join(&reference).display()
        );
        // A packed ref has no file of its own; packed-refs is where it lives instead.
        println!(
            "cargo:rerun-if-changed={}",
            git_dir.join("packed-refs").display()
        );
    }

    // The image build passes the sha in because its context has no .git. On a host, git is the
    // source of truth. Either way an unresolvable sha stays empty rather than guessing — the
    // consumers treat empty as "not stamped", which is a different thing from "stale".
    let sha = std::env::var("BORING_BUILD_SHA")
        .or_else(|_| std::env::var("BUILD_SHA"))
        .ok()
        .map(|s| s.trim().to_owned())
        .filter(|s| !s.is_empty())
        .or_else(git_head_sha)
        .unwrap_or_default();
    println!("cargo:rustc-env=BORING_BUILT_FROM={sha}");
}

/// The ref `.git/HEAD` points at, if it is a symbolic ref rather than a detached sha.
fn head_ref(head: &Path) -> Option<PathBuf> {
    let text = std::fs::read_to_string(head).ok()?;
    let reference = text.strip_prefix("ref:")?.trim();
    (!reference.is_empty()).then(|| PathBuf::from(reference))
}

fn git_head_sha() -> Option<String> {
    let out = Command::new("git")
        .args(["rev-parse", "HEAD"])
        .current_dir("..")
        .output()
        .ok()?;
    if !out.status.success() {
        return None;
    }
    let sha = String::from_utf8(out.stdout).ok()?.trim().to_owned();
    (!sha.is_empty()).then_some(sha)
}
