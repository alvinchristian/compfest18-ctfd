"""Cleanup support for solver files retained from earlier CTF deployments."""

from CTFd.utils.uploads import delete_file


def delete_solver_files_for_solves(solves_query):
    """Delete stored solver objects before their solve rows are removed."""
    from CTFd.models import SolverFiles, Solves

    solve_ids = [solve_id for solve_id, in solves_query.with_entities(Solves.id).all()]
    if not solve_ids:
        return
    solver_files = SolverFiles.query.filter(SolverFiles.solve_id.in_(solve_ids)).all()
    for solver_file in solver_files:
        delete_file(solver_file.id)
