"""Residual arithmetic must retain cancellation and multiplication errors."""

from decimal import Decimal, localcontext

import numpy as np
import pytest
from scipy.sparse import csr_matrix

pytest.importorskip("numba")
from dev.refine_inventory_solution import compensated_residual, refine


def test_compensated_residual_matches_decimal_float_inputs():
    matrix = csr_matrix([[1e16, 1, -1e16], [1.0000000000000002, 0, 0], [0, -2, 1]])
    solution = np.array([[1, 1.0000000000000002], [1, 0], [1, 0]], dtype=float)
    demand = np.array([[0, 0], [0, 1.0000000000000004], [0, 0]], dtype=float)
    actual = compensated_residual(
        matrix.indptr, matrix.indices, matrix.data, solution, demand
    )
    with localcontext() as ctx:
        ctx.prec = 80
        expected = np.array(
            [
                [
                    float(
                        Decimal.from_float(demand[i, k])
                        - sum(
                            Decimal.from_float(float(matrix[i, j]))
                            * Decimal.from_float(solution[j, k])
                            for j in range(3)
                        )
                    )
                    for k in range(2)
                ]
                for i in range(3)
            ]
        )
    np.testing.assert_array_equal(actual, expected)
    assert actual[0, 0] == -1
    assert actual[1, 1] < 0 and abs(actual[1, 1]) < 1e-30


def test_refinement_preserves_signed_solutions_and_zero_demands():
    matrix = csr_matrix([[2.0, -0.4, 0], [0, 1, 0.5], [-0.1, 0, 1]])
    demand = np.array([[0.0, 0], [0, 0], [1, 0]])
    initial = np.zeros_like(demand)
    solution, diagnostic = refine(matrix, demand, initial)
    np.testing.assert_allclose(
        solution, np.linalg.solve(matrix.toarray(), demand), rtol=1e-14
    )
    assert solution[1, 0] < 0
    assert np.array_equal(initial, np.zeros_like(initial))
    assert np.array_equal(solution[:, 1], np.zeros(3))
    assert (
        diagnostic["residual_history"][-1]["maximum_componentwise_backward_error"]
        < 1e-14
    )


def test_nonfinite_solution_is_rejected():
    with pytest.raises(ValueError, match="Nonfinite"):
        refine(csr_matrix(np.eye(2)), np.ones(2), np.array([1.0, np.nan]))
