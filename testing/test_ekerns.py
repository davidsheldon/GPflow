import unittest
import numpy as np
import numpy.random as rnd
import tensorflow as tf
import GPflow
from GPflow import kernels
from GPflow import ekernels

rnd.seed(0)


def _assert_pdeq(self, a, b, k=None):
    self.assertTrue(np.all(a.shape == b.shape))
    pdmax = np.max(np.abs(a / b - 1) * 100)
    # print("%s, %f" % (str(type(k)), pdmax))
    self.assertTrue(pdmax < self._threshold, msg="Percentage difference above threshold: %f\n"
                                                 "On kernel: %s" % (pdmax, str(type(k))))


def index_block(y, x, D):
    return np.s_[y * D:(y + 1) * D, x * D:(x + 1) * D]


class TriDiagonalBlockRep(GPflow.transforms.Transform):
    """
    Transforms an unconstrained representation of a PSD block tri diagonal matrix to its PSD block representation.
    """

    def __init__(self):
        GPflow.transforms.Transform.__init__(self)

    def forward(self, x):
        """
        Transforms from the free state to the matrix of blocks.
        :param x: Unconstrained state (Nx2DxD), where D is the block size.
        :return: Return PSD blocks (2xNxDxD)
        """
        N, D = x.shape[0], x.shape[2]
        diagblocks = np.einsum('nij,nik->njk', x, x)
        ob = np.einsum('nij,nik->njk', x[:-1, :, :], x[1:, :, :])
        # ob = np.einsum('nij,njk->nik', x[:-1, :, :].transpose([0, 2, 1]), x[1:, :, :])
        offblocks = np.vstack((ob, np.zeros((1, D, D))))
        return np.array([diagblocks, offblocks])

    def tf_forward(self, x):
        N, D = tf.shape(x)[0], tf.shape(x)[2]
        xm = tf.slice(x, [0, 0, 0], tf.pack([N - 1, -1, -1]))
        xp = x[1:, :, :]
        diagblocks = tf.batch_matmul(x, x, adj_x=True)
        offblocks = tf.concat(0, [tf.batch_matmul(xm, xp, adj_x=True), tf.zeros((1, D, D), dtype=tf.float64)])
        return tf.pack([diagblocks, offblocks])

    def __str__(self):
        return "BlockTriDiagonal"


class TestKernExpDelta(unittest.TestCase):
    """
    Check whether the normal kernel matrix is recovered if a delta distribution is used. First initial test which should
    indicate whether things work or not.
    """

    def setUp(self):
        self.D = 2
        self.Xmu = rnd.rand(10, self.D)
        self.Z = rnd.rand(4, self.D)
        self.Xcov = np.zeros((self.Xmu.shape[0], self.D, self.D))
        self.Xcovc = np.zeros((self.Xmu.shape[0], self.D, self.D))
        k1 = ekernels.RBF(self.D, ARD=True)
        k1.lengthscales = rnd.rand(2) + [0.5, 1.5]
        k1.variance = 0.3 + rnd.rand()
        klin = ekernels.Linear(self.D, variance=0.3 + rnd.rand())
        self.kernels = [k1, klin]

    def test_eKzxKxz(self):
        for k in self.kernels:
            psi2 = k.compute_eKzxKxz(self.Z, self.Xmu, self.Xcov)
            kernmat = k.compute_K(self.Z, self.Xmu)  # MxN
            kernouter = np.einsum('in,jn->nij', kernmat, kernmat)
            self.assertTrue(np.allclose(kernouter, psi2))

    def test_eKdiag(self):
        for k in self.kernels:
            kdiag = k.compute_eKdiag(self.Xmu, self.Xcov)
            orig = k.compute_Kdiag(self.Xmu)
            self.assertTrue(np.allclose(orig, kdiag))

    def test_exKxz(self):
        covall = np.array([self.Xcov, self.Xcovc])
        for k in self.kernels:
            if type(k) is ekernels.Linear:
                continue
            exKxz = k.compute_exKxz(self.Z, self.Xmu, covall)
            Kxz = k.compute_K(self.Xmu[:-1, :], self.Z)  # NxM
            xKxz = np.einsum('nm,nd->nmd', Kxz, self.Xmu[1:, :])
            self.assertTrue(np.allclose(xKxz, exKxz))

    def test_Kxz(self):
        for k in self.kernels:
            psi1 = k.compute_eKxz(self.Z, self.Xmu, self.Xcov)
            kernmat = k.compute_K(self.Z, self.Xmu)  # MxN
            self.assertTrue(np.allclose(kernmat, psi1.T))


class TestKernExpActiveDims(unittest.TestCase):
    _threshold = 0.5

    def setUp(self):
        self.N = 4
        self.D = 2
        self.Xmu = rnd.rand(self.N, self.D)
        self.Z = rnd.rand(3, self.D)
        unconstrained = rnd.randn(self.N, 2 * self.D, self.D)
        t = TriDiagonalBlockRep()
        self.Xcov = t.forward(unconstrained)

        variance = 0.3 + rnd.rand()

        k1 = ekernels.RBF(1, variance, active_dims=[0])
        k2 = ekernels.RBF(1, variance, active_dims=[1])
        klin = ekernels.Linear(1, variance, active_dims=[1])
        self.ekernels = [k1, k2, klin]  # Kernels doing the expectation in closed form, doing the slicing

        k1 = ekernels.RBF(1, variance)
        k2 = ekernels.RBF(1, variance)
        klin = ekernels.Linear(1, variance)
        self.pekernels = [k1, k2, klin]  # kernels doing the expectations in closed form, without slicing

        k1 = kernels.RBF(1, variance, active_dims=[0])
        klin = kernels.Linear(1, variance, active_dims=[1])
        self.kernels = [k1, klin]

        k1 = kernels.RBF(1, variance)
        klin = kernels.Linear(1, variance)
        self.pkernels = [k1, klin]

    def test_quad_active_dims(self):
        for k, pk in zip(self.kernels + self.ekernels, self.pkernels + self.pekernels):
            a = k.compute_eKdiag(self.Xmu, self.Xcov[0, :, :, :])
            sliced = np.take(np.take(self.Xcov, k.active_dims, axis=-1), k.active_dims, axis=-2)
            b = pk.compute_eKdiag(self.Xmu[:, k.active_dims], sliced[0, :, :, :])
            _assert_pdeq(self, a, b, k)

            a = k.compute_eKxz(self.Z, self.Xmu, self.Xcov[0, :, :, :])
            sliced = np.take(np.take(self.Xcov, k.active_dims, axis=-1), k.active_dims, axis=-2)
            b = pk.compute_eKxz(self.Z[:, k.active_dims], self.Xmu[:, k.active_dims], sliced[0, :, :, :])
            _assert_pdeq(self, a, b, k)

            a = k.compute_eKzxKxz(self.Z, self.Xmu, self.Xcov[0, :, :, :])
            sliced = np.take(np.take(self.Xcov, k.active_dims, axis=-1), k.active_dims, axis=-2)
            b = pk.compute_eKzxKxz(self.Z[:, k.active_dims], self.Xmu[:, k.active_dims], sliced[0, :, :, :])
            _assert_pdeq(self, a, b, k)


class TestExpxKxzActiveDims(unittest.TestCase):
    _threshold = 0.5

    def setUp(self):
        self.N = 4
        self.D = 2
        self.Xmu = rnd.rand(self.N, self.D)
        self.Z = rnd.rand(3, self.D)
        unconstrained = rnd.randn(self.N, 2 * self.D, self.D)
        t = TriDiagonalBlockRep()
        self.Xcov = t.forward(unconstrained)

        variance = 0.3 + rnd.rand()

        k1 = ekernels.RBF(1, variance, active_dims=[0])
        k2 = ekernels.RBF(1, variance, active_dims=[1])
        klin = ekernels.Linear(1, variance, active_dims=[1])
        self.ekernels = [k1, k2, klin]

        k1 = ekernels.RBF(2, variance)
        k2 = ekernels.RBF(2, variance)
        klin = ekernels.Linear(2, variance)
        self.pekernels = [k1, k2, klin]

        k1 = kernels.RBF(1, variance, active_dims=[0])
        klin = kernels.Linear(1, variance, active_dims=[1])
        self.kernels = [k1, klin]

        k1 = kernels.RBF(2, variance)
        klin = kernels.Linear(2, variance)
        self.pkernels = [k1, klin]

    def test_quad_active_dims(self):
        for k, pk in zip(self.kernels, self.pkernels):
            # exKxz is interacts slightly oddly with `active_dims`. It can't be implemented by simply dropping the
            # dependence on certain inputs. As we still need to output the outer product between x_{t-1} and K_{x_t, Z}.
            # So we can't do a comparison to a kernel that just takes a smaller X as an input. It may be possible to do
            # this though for a carefully crafted `Xcov`. However, I'll leave that as a todo for now.
            k.input_size = self.Xmu.shape[1]
            pk.input_size = self.Xmu.shape[1]
            a = k.compute_exKxz(self.Z, self.Xmu, self.Xcov)
            b = pk.compute_exKxz(self.Z, self.Xmu, self.Xcov)
            self.assertFalse(np.all(a == b))
            exp_shape = np.array([self.N - 1, self.Z.shape[0], self.D])
            self.assertTrue(np.all(a.shape == exp_shape),
                            msg="Shapes incorrect:\n%s vs %s" % (str(a.shape), str(exp_shape)))

        for k, pk in zip(self.ekernels, self.pekernels):
            try:
                k.compute_exKxz(self.Z, self.Xmu, self.Xcov)
                pk.compute_exKxz(self.Z, self.Xmu, self.Xcov)
            except Exception as e:
                self.assertTrue(type(e) is tf.errors.InvalidArgumentError)


class TestKernExpQuadrature(unittest.TestCase):
    _threshold = 0.5

    def setUp(self):
        self.rng = np.random.RandomState(0)
        self.N = 4
        self.D = 2
        self.Xmu = self.rng.rand(self.N, self.D)
        self.Z = self.rng.rand(2, self.D)

        unconstrained = rnd.randn(self.N, 2 * self.D, self.D)
        t = TriDiagonalBlockRep()
        self.Xcov = t.forward(unconstrained)

        # Set up "normal" kernels
        ekernel_classes = [ekernels.RBF, ekernels.Linear]
        kernel_classes = [kernels.RBF, kernels.Linear]
        params = [(self.D, 0.3 + self.rng.rand(), self.rng.rand(2) + [0.5, 1.5], None, True),
                  (self.D, 0.3 + self.rng.rand(), None)]
        self.ekernels = [c(*p) for c, p in zip(ekernel_classes, params)]
        self.kernels = [c(*p) for c, p in zip(kernel_classes, params)]

        # Test summed kernels
        rbfvariance = 0.3 + self.rng.rand()
        rbfard = [self.rng.rand() + 0.5]
        linvariance = 0.3 + self.rng.rand()
        self.kernels.append(
            kernels.Add([
                kernels.RBF(1, rbfvariance, rbfard, [1], False),
                kernels.Linear(1, linvariance, [0])
            ])
        )
        self.kernels[-1].input_size = self.kernels[-1].input_dim
        for k in self.kernels[-1].kern_list:
            k.input_size = self.kernels[-1].input_size
        # for k in self.kernels[-1].kern_list:
        #     k.num_gauss_hermite_points = 30
        self.ekernels.append(
            ekernels.Add([
                ekernels.RBF(1, rbfvariance, rbfard, [1], False),
                ekernels.Linear(1, linvariance, [0])
            ])
        )

    def test_eKdiag(self):
        for k, ek in zip(self.kernels, self.ekernels):
            a = k.compute_eKdiag(self.Xmu, self.Xcov[0, :, :, :])
            b = ek.compute_eKdiag(self.Xmu, self.Xcov[0, :, :, :])
            _assert_pdeq(self, a, b, k)

    def test_eKxz(self):
        for k, ek in zip(self.kernels, self.ekernels):
            a = k.compute_eKxz(self.Z, self.Xmu, self.Xcov[0, :, :, :])
            b = ek.compute_eKxz(self.Z, self.Xmu, self.Xcov[0, :, :, :])
            _assert_pdeq(self, a, b, k)

    def test_eKzxKxz(self):
        for k, ek in zip(self.kernels, self.ekernels):
            k._kill_autoflow()
            k.num_gauss_hermite_points = 30
            a = k.compute_eKzxKxz(self.Z, self.Xmu, self.Xcov[0, :, :, :])
            b = ek.compute_eKzxKxz(self.Z, self.Xmu, self.Xcov[0, :, :, :])
            _assert_pdeq(self, a, b, k)

    def test_exKxz(self):
        # TODO: Run test for sum kernel as well.
        for k, ek in zip(self.kernels[:-1], self.ekernels[:-1]):
            k._kill_autoflow()
            k.num_gauss_hermite_points = 20
            a = k.compute_exKxz(self.Z, self.Xmu, self.Xcov)
            b = ek.compute_exKxz(self.Z, self.Xmu, self.Xcov)
            _assert_pdeq(self, a, b, k)


if __name__ == '__main__':
    unittest.main()
