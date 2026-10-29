"""Server health checks: providers, players and the server log after boot."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scenarios.helpers import ma_log_errors

if TYPE_CHECKING:
    from scenarios.checks.context import CheckContext

BOOT_ISSUES = ("#6729",)
MAX_LOGGED_LINES = 20


async def boot_clean(ctx: CheckContext) -> None:
    """Check every provider loaded, every role registered and no error was logged at boot."""
    with ctx.step("providers_loaded", BOOT_ISSUES):
        missing = [prov.instance_id for prov in ctx.client.providers if not prov.available]
        assert not missing, f"providers not available: {missing}"

    with ctx.step("roles_registered", BOOT_ISSUES):
        missing = [
            role
            for role, player_id in ctx.players.items()
            if (player := ctx.client.players.get(player_id)) is None or not player.available
        ]
        assert not missing, f"roles without an available player: {missing}"

    with ctx.step("no_errors_logged", BOOT_ISSUES):
        # a tier 1 data dir is fresh, so the whole log is this boot
        errors = ma_log_errors(0)
        assert not errors, f"{len(errors)} error lines logged:\n" + "\n".join(
            errors[:MAX_LOGGED_LINES]
        )
