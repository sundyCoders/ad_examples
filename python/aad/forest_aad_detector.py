from copy import deepcopy
import numpy as np
from scipy.sparse import lil_matrix
from scipy import sparse
from scipy.sparse import csr_matrix, vstack

import logging

from common.utils import *
from aad.aad_globals import *
from common.sgd_optimization import *
from aad.aad_base import *
from aad.query_model import *
from aad.random_split_trees import *
from aad.aad_loss import *


class RegionData(object):
    def __init__(self, region, path_length, node_id, score, node_samples, log_frac_vol=0.0):
        self.region = region
        self.path_length = path_length
        self.node_id = node_id
        self.score = score
        self.node_samples = node_samples
        self.log_frac_vol = log_frac_vol


def is_in_region(x, region):
    d = len(x)
    for i in range(d):
        if not region[i][0] <= x[i] <= region[i][1]:
            return False
    return True


def is_forest_detector(detector_type):
    return (detector_type == AAD_IFOREST or
            detector_type == AAD_HSTREES or
            detector_type == AAD_RSFOREST)


def transform_features(x, all_regions, d):
    """ Inefficient method for looking up region membership.

    Note: This method is only for DEBUG. For a faster
    implementation, see below.
    @see: AadIsolationForest.transform_to_region_features

    :param x:
    :param all_regions:
    :param d:
    :return:
    """
    # translate x's to new coordinates
    x_new = np.zeros(shape=(x.shape[0], len(d)), dtype=np.float64)
    for i in range(x.shape[0]):
        for j, region in enumerate(all_regions):
            if is_in_region(x[i, :], region[0]):
                x_new[i, j] = d[j]
    return x_new


class AadForest(Aad, StreamingSupport):

    def __init__(self, n_estimators=10, max_samples=100, max_depth=10,
                 score_type=IFOR_SCORE_TYPE_INV_PATH_LEN,
                 ensemble_score=ENSEMBLE_SCORE_LINEAR,
                 random_state=None,
                 add_leaf_nodes_only=False,
                 detector_type=AAD_IFOREST, n_jobs=1):

        Aad.__init__(self, detector_type, ensemble_score, random_state)

        self.n_estimators = n_estimators
        self.max_samples = max_samples

        self.score_type = score_type
        if not (self.score_type == IFOR_SCORE_TYPE_INV_PATH_LEN or
                self.score_type == IFOR_SCORE_TYPE_INV_PATH_LEN_EXP or
                self.score_type == IFOR_SCORE_TYPE_CONST or
                self.score_type == IFOR_SCORE_TYPE_NEG_PATH_LEN or
                self.score_type == HST_SCORE_TYPE or
                self.score_type == RSF_SCORE_TYPE or
                self.score_type == RSF_LOG_SCORE_TYPE or
                self.score_type == ORIG_TREE_SCORE_TYPE):
            raise NotImplementedError("score_type %d not implemented!" % self.score_type)

        self.add_leaf_nodes_only = add_leaf_nodes_only

        if detector_type == AAD_IFOREST:
            self.clf = IForest(n_estimators=n_estimators, max_samples=max_samples,
                               n_jobs=n_jobs, random_state=self.random_state)
        elif detector_type == AAD_HSTREES:
            self.clf = HSTrees(n_estimators=n_estimators, max_depth=max_depth,
                               n_jobs=n_jobs, random_state=self.random_state)
        elif detector_type == AAD_RSFOREST:
            self.clf = RSForest(n_estimators=n_estimators, max_depth=max_depth,
                                n_jobs=n_jobs, random_state=self.random_state)
        else:
            raise ValueError("Incorrect detector type: %d. Only tree-based detectors (%d|%d|%d) supported." %
                             (detector_type, AAD_IFOREST, AAD_HSTREES, AAD_RSFOREST))

        # store all regions grouped by tree
        self.regions_in_forest = None

        # store all regions in a flattened list (ungrouped)
        self.all_regions = None

        # store maps of node index to region index for all trees
        self.all_node_regions = None

        # scores for each region
        self.d = None

        # samples for each region
        # self.node_samples = None

        # fraction of instances in each region
        # self.frac_insts = None

    def get_num_members(self):
        if self.d is not None:
            return len(self.d)
        return None

    def fit(self, x):
        tm = Timer()

        tm.start()
        self.clf.fit(x)
        # print len(clf.estimators_)
        # print type(clf.estimators_[0].tree_)

        logger.debug(tm.message("created original forest"))

        if self.score_type == ORIG_TREE_SCORE_TYPE:
            # no need to extract regions in this case
            return

        tm.start()
        self.regions_in_forest = []
        self.all_regions = []
        self.all_node_regions = []
        region_id = 0
        for i in range(len(self.clf.estimators_)):
            regions = self.extract_leaf_regions_from_tree(self.clf.estimators_[i],
                                                          self.add_leaf_nodes_only)
            self.regions_in_forest.append(regions)
            self.all_regions.extend(regions)
            node_regions = {}
            for region in regions:
                node_regions[region.node_id] = region_id
                region_id += 1  # this will monotonously increase across trees
            self.all_node_regions.append(node_regions)
            # print "%d, #nodes: %d" % (i, len(regions))
        self.d, _, _ = self.get_region_scores(self.all_regions)
        # self.w = self.get_uniform_weights()
        self.w_unif_prior = self.get_uniform_weights()
        logger.debug(tm.message("created forest regions"))

    def extract_leaf_regions_from_tree(self, tree, add_leaf_nodes_only=False):
        """Extracts leaf regions from decision tree.

        Returns each decision path as array of strings representing
        node comparisons.

        Args:
            tree: sklearn.tree
                A trained decision tree.
            add_leaf_nodes_only: bool
                whether to extract only leaf node regions or include 
                internal node regions as well

        Returns: list of
        """

        add_intermediate_nodes = not add_leaf_nodes_only

        left = tree.tree_.children_left
        right = tree.tree_.children_right
        features = tree.tree_.feature
        threshold = tree.tree_.threshold
        node_samples = tree.tree_.n_node_samples
        log_frac_vol = None
        if isinstance(tree.tree_, ArrTree):
            log_frac_vol = tree.tree_.acc_log_v

        # value = tree.tree_.value

        full_region = {}
        for fidx in range(tree.tree_.n_features):
            full_region[fidx] = (-np.inf, np.inf)

        regions = []

        def recurse(left, right, features, threshold, node, region, path_length=0):

            if left[node] == -1 and right[node] == -1:
                # we have reached a leaf node
                # print region
                regions.append(RegionData(deepcopy(region), path_length, node,
                                          self._average_path_length(node_samples[node]),
                                          node_samples[node],
                                          log_frac_vol=0. if log_frac_vol is None else log_frac_vol[node]))
                return
            elif left[node] == -1 or right[node] == -1:
                print "dubious node..."

            feature = features[node]

            if add_intermediate_nodes and node != 0:
                regions.append(RegionData(deepcopy(region), path_length, node,
                                          self._average_path_length(node_samples[node]),
                                          node_samples[node],
                                          log_frac_vol=0. if log_frac_vol is None else log_frac_vol[node]))

            if left[node] != -1:
                # make a copy to send down the next node so that
                # the previous value is unchanged when we backtrack.
                new_region = deepcopy(region)
                new_region[feature] = (new_region[feature][0], min(new_region[feature][1], threshold[node]))
                recurse(left, right, features, threshold, left[node], new_region, path_length + 1)

            if right[node] != -1:
                # make a copy for the reason mentioned earlier.
                new_region = deepcopy(region)
                new_region[feature] = (max(new_region[feature][0], threshold[node]), new_region[feature][1])
                recurse(left, right, features, threshold, right[node], new_region, path_length + 1)

        recurse(left, right, features, threshold, 0, full_region)
        return regions

    def _average_path_length(self, n_samples_leaf):
        """ The average path length in a n_samples iTree, which is equal to
        the average path length of an unsuccessful BST search since the
        latter has the same structure as an isolation tree.
        Parameters
        ----------
        n_samples_leaf : array-like of shape (n_samples, n_estimators), or int.
            The number of training samples in each test sample leaf, for
            each estimators.

        Returns
        -------
        average_path_length : array, same shape as n_samples_leaf

        """
        if n_samples_leaf <= 1:
            return 1.
        else:
            return 2. * (np.log(n_samples_leaf) + 0.5772156649) - 2. * (
                n_samples_leaf - 1.) / n_samples_leaf

    def decision_path_full(self, x, tree):
        """Returns the node ids of all nodes from root to leaf for each sample (row) in x
        
        Args:
            x: numpy.ndarray
            tree: fitted decision tree
        
        Returns: list of length x.shape[0]
            list of lists
        """

        left = tree.tree_.children_left
        right = tree.tree_.children_right
        features = tree.tree_.feature
        threshold = tree.tree_.threshold

        def path_recurse(x, left, right, features, threshold, node, path_nodes):
            """Returns the node ids of all nodes that x passes through from root to leaf
            
            Args:
                x: numpy.array
                    a single instance
                path_nodes: list
            """

            if left[node] == -1 and right[node] == -1:
                # reached a leaf
                return
            else:
                feature = features[node]
                if x[feature] <= threshold[node]:
                    next_node = left[node]
                else:
                    next_node = right[node]
                path_nodes.append(next_node)
                path_recurse(x, left, right, features, threshold, next_node, path_nodes)

        n = x.shape[0]
        all_path_nodes = []
        for i in xrange(n):
            path_nodes = []
            path_recurse(x[i, :], left, right, features, threshold, 0, path_nodes)
            all_path_nodes.append(path_nodes)
        return all_path_nodes

    def decision_path_leaf(self, x, tree):
        n = x.shape[0]
        all_path_nodes = []

        # get all leaf nodes
        node_idxs = tree.apply(x)
        # logger.debug("node_idxs:\n%s" % str(node_idxs))

        for j in range(n):
            all_path_nodes.append([node_idxs[j]])

        return all_path_nodes

    def get_decision_path(self, x, tree):
        if self.add_leaf_nodes_only:
            return self.decision_path_leaf(x, tree)
        else:
            return self.decision_path_full(x, tree)

    def get_region_scores(self, all_regions):
        """Larger values mean more anomalous"""
        d = np.zeros(len(all_regions))
        node_samples = np.zeros(len(all_regions))
        frac_insts = np.zeros(len(all_regions))
        for i, region in enumerate(all_regions):
            node_samples[i] = region.node_samples
            frac_insts[i] = region.node_samples * 1.0 / self.max_samples
            if self.score_type == IFOR_SCORE_TYPE_INV_PATH_LEN:
                d[i] = 1. / region.path_length
            elif self.score_type == IFOR_SCORE_TYPE_INV_PATH_LEN_EXP:
                d[i] = 2 ** -region.path_length  # used this to run the first batch
            elif self.score_type == IFOR_SCORE_TYPE_CONST:
                d[i] = -1
            elif self.score_type == IFOR_SCORE_TYPE_NEG_PATH_LEN:
                d[i] = -region.path_length
            elif self.score_type == HST_SCORE_TYPE:
                d[i] = -region.node_samples * (2. ** region.path_length)
                # d[i] = -region.node_samples * region.path_length
                # d[i] = -np.log(region.node_samples + 1) + region.path_length
            elif self.score_type == RSF_SCORE_TYPE:
                d[i] = -region.node_samples * np.exp(region.log_frac_vol)
            elif self.score_type == RSF_LOG_SCORE_TYPE:
                d[i] = -np.log(region.node_samples + 1) - region.log_frac_vol
            else:
                # if self.score_type == IFOR_SCORE_TYPE_NORM:
                raise NotImplementedError("score_type %d not implemented!" % self.score_type)
                # d[i] = frac_insts[i]  # RPAD-ish
                # depth = region.path_length - 1
                # node_samples_avg_path_length = region.score
                # d[i] = (
                #            depth + node_samples_avg_path_length
                #        ) / (self.n_estimators * self._average_path_length(self.clf._max_samples))
        return d, node_samples, frac_insts

    def get_score(self, x, w=None):
        """Higher score means more anomalous"""
        #if self.score_type == IFOR_SCORE_TYPE_INV_PATH_LEN or \
        #                self.score_type == IFOR_SCORE_TYPE_INV_PATH_LEN_EXP or \
        #                self.score_type == IFOR_SCORE_TYPE_CONST or \
        #                self.score_type == IFOR_SCORE_TYPE_NEG_PATH_LEN or \
        #                self.score_type == HST_SCORE_TYPE:
        if w is None:
            w = self.w
        if w is None:
            raise ValueError("weights not initialized")
        if self.ensemble_score == ENSEMBLE_SCORE_LINEAR:
            return x.dot(w)
        elif self.ensemble_score == ENSEMBLE_SCORE_EXPONENTIAL:
            # return np.exp(x.dot(w))
            raise NotImplementedError("score_type %d not supported!" % self.score_type)
        else:
            raise NotImplementedError("score_type %d not implemented!" % self.score_type)

    def decision_function(self, x):
        """Returns the decision function for the original underlying classifier"""
        return self.clf.decision_function(x)

    def supports_streaming(self):
        return self.clf.supports_streaming()

    def add_samples(self, X, current=False):
        """Incrementally updates the stream buffer node counts"""
        if not self.supports_streaming():
            # raise ValueError("Detector does not support incremental update")
            logger.warning("Model does not support stream update. Retaining old model.")
        if current:
            raise ValueError("Only current=False supported")
        self.clf.add_samples(X, current=current)

    def update_region_scores(self):
        for i, estimator in enumerate(self.clf.estimators_):
            tree = estimator.tree_
            node_regions = self.all_node_regions[i]
            for node_id in node_regions:
                region_id = node_regions[node_id]
                self.all_regions[region_id].node_samples = tree.n_node_samples[node_id]
        self.d, _, _ = self.get_region_scores(self.all_regions)

    def update_model_from_stream_buffer(self):
        self.clf.update_model_from_stream_buffer()
        #for i, estimator in enumerate(self.clf.estimators_):
        #    estimator.tree.tree_.update_model_from_stream_buffer()
        self.update_region_scores()

    def get_region_score_for_instance_transform(self, region_id, norm_factor=1.0):
        if (self.score_type == IFOR_SCORE_TYPE_CONST or
                    self.score_type == HST_SCORE_TYPE or
                    self.score_type == RSF_SCORE_TYPE or
                    self.score_type == RSF_LOG_SCORE_TYPE):
            return self.d[region_id]
        elif self.score_type == ORIG_TREE_SCORE_TYPE:
            raise ValueError("Score type %d not supported for method get_region_score_for_instance_transform()" % self.score_type)
        else:
            return self.d[region_id] / norm_factor

    def transform_to_ensemble_features(self, x, dense=False, norm_unit=False):
        """ Transforms matrix x to features from isolation forest

        :param x: np.ndarray
            Input data in original feature space
        :param dense: bool
            Whether to return a dense matrix or sparse. The number
            of features in isolation forest correspond to the nodes
            which might be thousands in number. However, each instance
            (row) in x will have only as many non-zero values as the
            number of trees -- which is *much* smaller than the number
            of nodes.
        :param norm_unit: bool
            Whether to normalize the transformed instance vectors to unit length
        :return:
        """
        if dense:
            return self.transform_to_region_features_dense(x, norm_unit=norm_unit)
        else:
            return self.transform_to_region_features_sparse(x, norm_unit=norm_unit)

    def transform_to_region_features_dense(self, x, norm_unit=False):
        # return transform_features(x, self.all_regions, self.d)
        x_new = np.zeros(shape=(x.shape[0], len(self.d)), dtype=float)
        self._transform_to_region_features_with_lookup(x, x_new)
        return x_new

    def transform_to_region_features_sparse(self, x, norm_unit=False):
        """ Transforms from original feature space to IF node space
        
        The conversion to sparse vectors seems to take a lot of intermediate
        memory in python. This is why we are converting the vectors in smaller
        batches. The transformation is a one-time task, hence not a concern in 
        most cases.
        
        :param x: 
        :return: 
        """
        # logger.debug("transforming to IF feature space...")
        n = x.shape[0]
        m = len(self.d)
        batch_size = 10000
        start_batch = 0
        end_batch = min(start_batch + batch_size, n)
        x_new = csr_matrix((0, m), dtype=float)
        while start_batch < end_batch:
            starttime = timer()
            x_tmp = matrix(x[start_batch:end_batch, :], ncol=x.shape[1])
            x_tmp_new = lil_matrix((end_batch - start_batch, m), dtype=x_new.dtype)
            for i, tree in enumerate(self.clf.estimators_):
                n_tmp = x_tmp.shape[0]
                node_regions = self.all_node_regions[i]
                tree_paths = self.get_decision_path(x_tmp, tree)
                for j in xrange(n_tmp):
                    k = len(tree_paths[j])
                    for node_idx in tree_paths[j]:
                        region_id = node_regions[node_idx]
                        x_tmp_new[j, region_id] = self.get_region_score_for_instance_transform(region_id, k)
            if n >= 100000:
                endtime = timer()
                tdiff = difftime(endtime, starttime, units="secs")
                logger.debug("processed %d/%d (%f); batch %d in %f sec(s)" %
                             (end_batch + 1, n, (end_batch + 1)*1./n, batch_size, tdiff))
            if norm_unit:
                norms = np.sqrt(x_tmp_new.power(2).sum(axis=1))
                zero_idxs = np.where(norms == 0)[0]
                if len(zero_idxs) > 0:
                    # in order to avoid a divide by zero warning
                    norms[zero_idxs] = 1
                # logger.debug("norms before [%d/%d]:\n%s" % (start_batch, end_batch, str(list(norms.T))))
                x_tmp_new = x_tmp_new.multiply(1/norms)
                # norms = np.sqrt(x_tmp_new.power(2).sum(axis=1))
                # logger.debug("norms after [%d/%d]:\n%s" % (start_batch, end_batch, str(list(norms.T))))
            x_new = vstack([x_new, x_tmp_new.tocsr()])
            start_batch = end_batch
            end_batch = min(start_batch + batch_size, n)
        return x_new

    def _transform_to_region_features_with_lookup(self, x, x_new):
        """ Transforms from original feature space to IF node space

        NOTE: This has been deprecated. Will be removed in future.

        Performs the conversion tree-by-tree. Even with batching by trees,
        this requires a lot of intermediate memory. Hence we do not use this method...

        :param x:
        :param x_new:
        :return:
        """
        starttime = timer()
        n = x_new.shape[0]
        for i, tree in enumerate(self.clf.estimators_):
            node_regions = self.all_node_regions[i]
            for j in range(n):
                tree_paths = self.get_decision_path(matrix(x[j, :], nrow=1), tree)
                k = len(tree_paths[0])
                for node_idx in tree_paths[0]:
                    region_id = node_regions[node_idx]
                    x_new[j, region_id] = self.get_region_score_for_instance_transform(region_id, k)
                if j >= 100000:
                    if j % 20000 == 0:
                        endtime = timer()
                        tdiff = difftime(endtime, starttime, units="secs")
                        logger.debug("processed %d/%d trees, %d/%d (%f) in %f sec(s)" %
                                     (i, len(self.clf.estimators_), j + 1, n, (j + 1)*1./n, tdiff))

