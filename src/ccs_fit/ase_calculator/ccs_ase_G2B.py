# ------------------------------------------------------------------------------#
#  CCS: Curvature Constrained Splines                                          #
#  Copyright (C) 2019 - 2023  CCS developers group                             #
#                                                                              #
#  See the LICENSE file for terms of usage and distribution.                   #
# ------------------------------------------------------------------------------#

# import logging
import sympy
import numpy as np
import itertools as it
from collections import OrderedDict, defaultdict
from numpy import linalg as LA
from ase.calculators.calculator import Calculator, all_changes
from ase.constraints import full_3x3_to_voigt_6_stress

try:
    from pymatgen.core import Lattice, Structure
    from pymatgen.analysis import ewald
except:
    pass

# logging.basicConfig(filename="ccs.spl", level=logging.DEBUG)
# logg = logging.getLogger(__name__)


class G2B_pair:
    def __init__(self, elem1, elem2, G2B_params, exp=True):
        self.elem1 = elem1
        self.elem2 = elem2
        self.no_pair = False
        try:
            pair = elem1 + "-" + elem2
            self.rcut = G2B_params["Two_body"][pair]["r_cut"]
        except:
            try:
                pair = elem2 + "-" + elem1
                self.rcut = G2B_params["Two_body"][pair]["r_cut"]
            except:
                self.rcut = 0.0
                self.no_pair = True
        if self.no_pair:
            self.Rmin = 0.0
            self.rcut = 0.0
            self.V_func  = None
            self.F_func = None

        else:
            self.rcut = G2B_params["Two_body"][pair]["r_cut"]
            func = G2B_params["Two_body"][pair]["V_func"]
            self.V_func = sympy.sympify(func)
            r_ij = sympy.Symbol('r_ij')
            self.F_func = sympy.diff(self.V_func, r_ij) 
            #print(self.F_func)
            #print(self.V_func)

    def eval_energy(self, r):
        if self.no_pair:
            val=0.0
        else:
            if r <= self.rcut:
                f_eval=self.V_func.subs({'r_ij':r})
                val=f_eval.evalf()    
            else:
                val = 0.0
        return float(val)

    def eval_force(self, r):
        if self.no_pair:
            val=0.0
        else:
            if r <= self.rcut:
                f_eval=self.F_func.subs({'r_ij':r})
                val=f_eval.evalf()    
            else:
                val = 0.0
        return float(val)

def ew(atoms, q):

    #   structure = AseAtomsAdaptor.get_structure(atoms)
    atoms.charges = []
    for a in atoms.get_chemical_symbols():
        atoms.charges.append(q[a])
    lattice = Lattice(atoms.get_cell())
    coords = atoms.get_scaled_positions()
    struct = Structure(
        lattice,
        atoms.get_chemical_symbols(),
        coords,
        site_properties={"charge": atoms.charges},
    )
    Ew = ewald.EwaldSummation(struct, compute_forces=True)
    return Ew


class G2B(Calculator):
    """
    General analyical 2B calculator

    Curvature constrained splines calculator compatible with the ASE
    format.

    Parameters
    ----------
    input_file : G2B.params
        To be added, Jolla.

    Returns
    -------
    What does it return actually, Jolla?


    Examples
    --------
    >>> To be added, Jolla.
    """

    implemented_properties = {"stress", "energy", "forces"}

    def __init__(
        self, G2B_params=None, charge=None, q=None, charge_scaling=False, **kwargs
    ):
        self.rc = 7.0  # SET THIS MAX OF ANY PAIR
        self.charge = charge
        self.species = None
        self.pair = None
        self.q = q
        self.G2B_params = G2B_params
        self.eps = G2B_params["One_body"]
        if charge_scaling:
            for key in self.q:
                self.q[key] *= self.G2B_params["Charge scaling factor"]

        Calculator.__init__(self, **kwargs)

    def calculate(self, atoms=None, properties=["energy"], system_changes=all_changes):
        Calculator.calculate(self, atoms, properties, system_changes)
        self.species = list(set(self.atoms.get_chemical_symbols()))
        self.pair = dict()
        for a, b in it.product(self.species, self.species):
            self.pair[a + b] = G2B_pair(a, b, self.G2B_params)

        if self.atoms.number_of_lattice_vectors == 3:
            cell = atoms.get_cell()
            self.atoms.wrap()
            n_repeat = self.rc * np.linalg.norm(np.linalg.inv(cell), axis=0)
            n_repeat = np.ceil(n_repeat).astype(int)
            offsets = [*it.product(*[np.arange(-n, n + 1) for n in n_repeat])]

        natoms = len(self.atoms)
        dict_species = defaultdict(int)
        for elem in self.atoms.get_chemical_symbols():
            dict_species[elem] += 1

        energy = 0.0
        forces = np.zeros((natoms, 3))
        stresses = np.zeros((natoms, 3, 3))

        # ONE-BODY ENERGY
        elems = it.combinations_with_replacement(dict_species.keys(), 1)
        for elem in elems:
            try:
                energy += self.eps[elem[0]] * dict_species[elem[0]]
            except:
                pass

        # PAIR-WISE ENERGY AND FORCE
        for x, y in it.product(self.species, self.species):
            xy_distances = []
            mask1 = [atom == x for atom in self.atoms.get_chemical_symbols()]
            mask2 = [atom == y for atom in self.atoms.get_chemical_symbols()]
            pos1 = self.atoms[mask1].positions
            index1 = np.arange(0, len(self.atoms))[mask1]
            atoms_2 = self.atoms[mask2]
            if self.atoms.number_of_lattice_vectors == 3:
                pos2 = []
                for offset in offsets:
                    pos2.append((atoms_2.positions + offset @ cell))
            else:
                pos2 = list(atoms_2.positions)
            pos2 = np.array(pos2)
            pos2 = np.reshape(pos2, (-1, 3))
            for p1, id in zip(pos1, index1):
                dist = pos2 - p1
                norm_dist = np.linalg.norm(dist, axis=1)
                dist_mask = (norm_dist < self.rc) & (norm_dist > 0)
                xy_distances.extend(norm_dist[dist_mask].tolist())
                # Sometimes there are no distances to append
                # Force calculation
                try:
                    forces[id, :] += np.sum(
                        (
                            dist[dist_mask].T
                            * list(
                                map(self.pair[x + y].eval_force, norm_dist[dist_mask])
                            )
                            / norm_dist[dist_mask]
                        ).T,
                        axis=0,
                    )
                except:
                    pass

                # Stress calculation
                id2s = [i for i, x in enumerate(dist_mask) if x]
                if norm_dist != []:
                    for id2 in id2s:
                        cur_f = self.pair[x + y].eval_force(norm_dist[id2])*dist[id2, :]/norm_dist[id2]
                        cur_dist = dist[id2, :]
                        cur_stress = np.outer(cur_f, cur_dist)
                        # print(cur_f, cur_dist, cur_stress)
                        stresses[id, :, :] += cur_stress

            energy += 0.5 * sum(map(self.pair[x + y].eval_energy, xy_distances))

        if self.charge:
            ewa = ew(self.atoms, self.q)
            energy = energy + ewa.total_energy
            forces = forces + ewa.forces

        self.results["energy"] = energy
        self.results["free_energy"] = energy
        self.results["forces"] = forces

        if self.atoms.number_of_lattice_vectors == 3:
            stresses = full_3x3_to_voigt_6_stress(stresses)
            self.results["stress"] = stresses.sum(axis=0) / self.atoms.get_volume()
            self.results["stresses"] = stresses / self.atoms.get_volume()
