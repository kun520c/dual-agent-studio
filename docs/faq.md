# FAQ

**Is this a stable installer?** No. It is a source development preview, GUI v0.14.1 with the B2.2-A backend. No binary Release is supplied. Build `frontend/dist` yourself after clone; none is published. Doc milestones (GUI / backend) are not the same as `dual_agent.__version__` (`0.2.0` today).

**Do I need Codex, Cursor or an API key?** Not for the fixed Mock rehearsal or protected offline tests. Real use requires separate explicit authorization and trusted deployment configuration; this source carries no production signer.

**Why is the GUI empty?** Original history is intentionally excluded. Create the built-in safe rehearsal in 项目管理. A registered arbitrary repository does not gain permission to execute.

**Missing frontend assets?** Run npm ci and the check/build commands through `design-prototype`. The old directory name is kept for import compatibility. Do not copy someone else's node_modules or private frontend build.

**Junction or host tests fail?** Use a local NTFS path with normal permissions and Git available. Restricted sandboxes or shared folders may reject Junctions/worktrees. Preserve failures and report them; do not disable safety checks to make CI green.

**Is API Manager production-validated?** No. The 59 API tests are offline engineering validation. B2.2-B live API integration has not run. OpenAI and DeepSeek use separate profiles; their live account/service availability is unknown here.

**Does verified mean a model said yes?** No. It requires independent current tool evidence and legal acceptance. Historical evidence does not grant new authority.

**Can I freely reuse the source?** A project license has not been chosen. Public visibility does not grant a general reuse or redistribution license. Dependencies have separate terms.
