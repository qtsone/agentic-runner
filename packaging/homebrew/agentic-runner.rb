# The workstation Runner's Homebrew formula (PRD issue 47, map ticket 23 item 1), for the
# qtsone tap. HEAD-only until the Runner is published as a release artifact (issue 46's
# extraction): `brew install --HEAD qtsone/tap/agentic-runner`.
#
# No `service do` block, on purpose. `brew services` manages one LaunchAgent per formula,
# and the workstation Runner is one process per Organisation (23 item 3), so
# `agentic-runner install <org>` writes each Organisation's own LaunchAgent into
# ~/Library/LaunchAgents -- the same per-user location, loaded into the same `gui/<uid>`
# domain `brew services` uses. Never a LaunchDaemon, never sudo.
class AgenticRunner < Formula
  include Language::Python::Virtualenv

  desc "Agentic OS Runner: one per-user login agent per Organisation"
  homepage "https://github.com/qtsone/agentic-runner"
  license "AGPL-3.0-only"
  head "https://github.com/qtsone/agentic-runner.git", branch: "main"

  depends_on "python@3.12"

  def install
    virtualenv_create(libexec, "python3.12")
    system libexec/"bin/python", "-m", "pip", "install",
           buildpath/"packages/contracts", buildpath/"packages/runner"
    bin.install_symlink libexec/"bin/agentic-runner"
  end

  def caveats
    <<~EOS
      Install one Runner per Organisation, as yourself (never with sudo):
        agentic-runner install <org> \\
          --control-plane https://<control plane> \\
          --temporal-address temporal-grpc.<zone>:443
      You are asked for the Agent Token your Organisation's Admin issued to you.
      `codex` and `claude` are found on your PATH, not bundled.
      Then: agentic-runner status|stop|start <org>
    EOS
  end

  test do
    assert_match "agentic-runner", shell_output("#{bin}/agentic-runner --version")
  end
end
