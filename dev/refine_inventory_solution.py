"""Refine inventory solutions with compensated double-precision residuals.

Apple arm64 NumPy longdouble has the same precision as float64. Use Dekker's
product error and Neumaier summation instead of assuming extended precision.
The matrix, demands, signs and units are unchanged. Numba compiles the residual
loop without fast-math reassociation; the correction uses a separate sparse LU.
"""

import numpy as np
from numba import njit
from scipy.sparse.linalg import splu


# The same file is imported both as dev.refine_inventory_solution and as a
# standalone script helper after chdir; cached Numba environments embed the
# import name and are not portable between those entry points.
@njit
def compensated_residual(indptr, indices, data, solution, demand):
    residual = demand.copy()
    for row in range(demand.shape[0]):
        for column in range(demand.shape[1]):
            total, correction = demand[row, column], 0.0
            for position in range(indptr[row], indptr[row + 1]):
                left = -data[position]
                right = solution[indices[position], column]
                product = left * right
                split = 134217729.0 * left
                left_hi = split - (split - left)
                left_lo = left - left_hi
                split = 134217729.0 * right
                right_hi = split - (split - right)
                right_lo = right - right_hi
                product_error = (
                    ((left_hi * right_hi - product) + left_hi * right_lo)
                    + left_lo * right_hi
                ) + left_lo * right_lo
                updated = total + product
                if abs(total) >= abs(product):
                    correction += (total - updated) + product
                else:
                    correction += (product - updated) + total
                correction += product_error
                total = updated
            residual[row, column] = total + correction
    return residual


def refine(matrix, demand, solution, iterations=3):
    """Return corrected supplies and diagnostics without modifying inputs."""
    matrix = matrix.tocsr().astype(np.float64)
    matrix.sum_duplicates()
    matrix.sort_indices()
    vector = np.asarray(demand).ndim == 1
    rhs = np.asarray(demand, dtype=np.float64)
    result = np.array(solution, dtype=np.float64, copy=True)
    if vector:
        rhs, result = rhs[:, None], result[:, None]
    if (
        matrix.shape[0] != matrix.shape[1]
        or rhs.shape != result.shape
        or rhs.shape[0] != matrix.shape[0]
    ):
        raise ValueError("Incompatible inventory, demand and solution shapes")
    if not all(np.isfinite(x).all() for x in (matrix.data, rhs, result)):
        raise ValueError("Nonfinite input to inventory refinement")
    # Transposed factorization follows SuperLU's efficient CSR solve orientation.
    # It avoids the large fill-in of a direct CSC factorization on these models.
    factor = splu(matrix.T.tocsc())
    history = []
    matrix_norm = float(abs(matrix).sum(axis=1).max())
    for step in range(iterations + 1):
        residual = compensated_residual(
            matrix.indptr, matrix.indices, matrix.data, result, rhs
        )
        scale = abs(matrix) @ abs(result) + abs(rhs)
        if not np.isfinite(residual).all() or not np.isfinite(scale).all():
            raise ValueError("Nonfinite residual during inventory refinement")
        error = np.divide(
            abs(residual), scale, out=np.zeros_like(residual), where=scale != 0
        )
        denominator = matrix_norm * np.max(abs(result), axis=0) + np.max(
            abs(rhs), axis=0
        )
        normwise = np.divide(
            np.max(abs(residual), axis=0),
            denominator,
            out=np.zeros_like(denominator),
            where=denominator != 0,
        )
        history.append(
            {
                "step": step,
                "maximum_absolute_residual": float(np.max(abs(residual))),
                "maximum_componentwise_backward_error": float(np.max(error)),
                "maximum_normwise_backward_error": float(np.max(normwise)),
            }
        )
        if step < iterations:
            result += factor.solve(residual, trans="T")
    if (
        not np.isfinite(result).all()
        or history[-1]["maximum_normwise_backward_error"] > 1e-12
    ):
        raise ValueError("Inventory refinement did not reach a reliable solution")
    return (result[:, 0] if vector else result), {
        "algorithm": f"{iterations} residual corrections; Dekker products, Neumaier sums and transposed SuperLU factorization; float64 inputs unchanged",
        "residual_history": history,
    }
