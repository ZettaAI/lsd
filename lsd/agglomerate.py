from scipy.ndimage.measurements import find_objects
from skimage.future.graph import RAG
from graph_merge import merge_hierarchical
import gunpowder as gp
import numpy as np
import logging
import math

logger = logging.getLogger(__name__)

class LsdAgglomeration(object):
    '''Create a local shape descriptor agglomerator.

    Args:

        fragments (``np.ndarray``):

            Initial fragments.

        target_lsds (``np.ndarray``):

            The local shape descriptors to match.

        lsd_extractor (``LsdExtractor``):

            The local shape descriptor object used to compute the difference
            between the segmentation and the target LSDs.

        voxel_size (``tuple`` of ``int``, optional):

            The voxel size of ``fragments``. Defaults to 1.

        keep_lsds (``bool``, optional):

            If ``True``, keeps the reconstructed local shape descriptors, which
            can then be queried with `func:get_lsds`.
    '''

    def __init__(
            self,
            fragments,
            target_lsds,
            lsd_extractor,
            voxel_size=None):

        self.segmentation = np.array(fragments)
        self.lsds = np.zeros_like(target_lsds)
        self.fragments = fragments
        self.target_lsds = target_lsds
        self.lsd_extractor = lsd_extractor
        self.context = lsd_extractor.get_context()

        if voxel_size is None:
            self.voxel_size = (1,)*len(fragments.shape)
        else:
            self.voxel_size = voxel_size

        self.__initialize_rag()

    def merge_until(self, threshold, max_merges=-1):
        '''Merge until the given threshold. Since edges are scored by how much
        they decrease the distance to ``target_lsds``, a threshold of 0 should
        be optimal.'''

        logger.info("Merging until %f...", threshold)

        merge_func = lambda _, src, dst: self.__merge_nodes(src, dst)
        weight_func = lambda _g, _s, u, v: self.__score_merge(u, v)
        num_merges = merge_hierarchical(
            self.fragments,
            self.rag,
            thresh=threshold,
            rag_copy=False,
            in_place_merge=True,
            merge_func=merge_func,
            weight_func=weight_func,
            max_merges=max_merges,
            return_segmenation=False)

        logger.info("Finished merging")

        return num_merges

    def get_segmentation(self):
        '''Return the segmentation obtained so far by calls to
        ``merge_until``.'''

        return self.segmentation

    def get_lsds(self):
        '''Return the local shape descriptors corresponding to the current
        segmentation.'''
        return self.lsds

    def __initialize_rag(self):

        self.rag = RAG(self.fragments, connectivity=2)
        logger.info(
            "RAG contains %d nodes and %d edges",
            len(self.rag.nodes()),
            len(self.rag.edges()))

        logger.info("Computing LSDs for initial fragments...")

        for u in self.rag.nodes():

            logger.debug("Initializing node %d", u)

            bb = find_objects(self.fragments==u)[0]
            self.rag.node[u]['roi'] = self.__slice_to_roi(bb)
            self.rag.node[u]['score'] = self.__compute_node_score(u)
            self.rag.node[u]['labels'] = [u] # needed by scikit

            logger.debug("Node %d: %s", u, self.rag.node[u])

        logger.info("Scoring initial edges...")

        for (u, v) in self.rag.edges():
            logger.debug("Initializing edge (%d, %d)", u, v)
            score = self.__score_merge(u, v)
            self.rag[u][v]['weight'] = score['weight']

    def __score_merge(self, u, v):
        '''Callback for merge_hierarchical, called to get the weight of a new
        edge.'''

        weight = self.__compute_edge_score(u, v)

        logger.debug("Scoring merge between %d and %d with %f", u, v, weight)

        return {'weight': weight}

    def __compute_node_score(self, u):
        '''Compute the LSDs score for a node.

        The node score is the sum of squared differences between the node LSDs
        and the target LSDs.

        This also stores the node's LSDs in self.lsds.
        '''

        # get ROI
        roi = self.rag.node[u]['roi']

        # get slice of segmentation for roi
        segmentation = self.segmentation[roi.to_slices()]

        # get LSDs for u
        lsds = self.lsd_extractor.get_descriptors(
            segmentation,
            labels=[u],
            voxel_size=self.voxel_size)

        # subtract from target LSDs
        u_mask = segmentation == u
        lsds_slice = (slice(None),) + roi.to_slices()
        diff = self.target_lsds[lsds_slice] - lsds
        diff[:,u_mask==0] = 0

        # update LSDs for u
        self.lsds[lsds_slice][:,u_mask] = lsds[:,u_mask]

        return np.sum(diff**2)

    def __merge_nodes(self, u, v):
        '''Merge node u into v.

        This does not change the graph (this is taken care of by the
        hierarchical agglomeration).

        This updates the segmentation (u is replaced by v), the LSDs of the
        current segmentaion, the ROI of v, and computes the new score for v.
        '''

        self.__merge_segmentation(u, v)

        (change_roi, context_roi) = self.__get_lsds_edge_rois(u, v)

        # get slice of segmentation for context_roi (no copy, we want to keep
        # the changes made)
        segmentation = self.segmentation[context_roi.to_slices()]

        # slices to cut change ROI from LSDs
        lsds_slice = (slice(None),) + change_roi.to_slices()

        # change ROI relative to context ROI
        change_in_context_roi = change_roi - context_roi.get_begin()

        # get LSDs for (u + v)
        lsds_merged = self.lsd_extractor.get_descriptors(
            segmentation,
            roi=change_in_context_roi,
            labels=[v],
            voxel_size=self.voxel_size)

        # update LSDs (only where segmentation == v)
        v_mask = segmentation[change_in_context_roi.to_slices()] == v
        self.lsds[lsds_slice][:,v_mask] = lsds_merged[:,v_mask]

        # set the ROI of v to the union of u and v
        roi_u = self.rag.node[u]['roi']
        roi_v = self.rag.node[v]['roi']
        self.rag.node[v]['roi'] = roi_u.union(roi_v)

        # update node score
        self.rag.node[v]['score'] = (
            self.rag.node[v]['score'] +
            self.rag.node[u]['score'] +
            self.rag[u][v]['weight'])

        logger.info(
            "Merged %d into %d with score %f",
            u, v, self.rag[u][v]['weight'])
        logger.debug(
            "Updated score of %d (merged with %d) to %f",
            u, v, self.rag.node[v]['score'])

    def __merge_segmentation(self, u, v):
        '''Replace u with v in segmentation.'''

        segmentation_u = self.segmentation[self.rag.node[u]['roi'].to_slices()]
        segmentation_u[segmentation_u==u] = v

    def __compute_edge_score(self, u, v):
        '''Compute the LSDs score for an edge.

        The edge score is by how much the incident node scores would improve
        when merged (negative if the score decreases). More formally, it is:

            s(u + v) - (s(u) + s(v))

        where s(.) is the score of a node and (u + v) is a node obtained from
        merging u and v.
        '''

        (change_roi, context_roi) = self.__get_lsds_edge_rois(u, v)

        # get slice of segmentation for context_roi (make a copy, since we
        # change it later)
        segmentation = self.segmentation[context_roi.to_slices()]
        segmentation = np.array(segmentation)

        # slices to cut change ROI from LSDs
        lsds_slice = (slice(None),) + change_roi.to_slices()

        # change ROI relative to context ROI
        change_in_context_roi = change_roi - context_roi.get_begin()

        # mask for voxels in u and v for change ROI
        not_uv_mask = np.logical_not(
            np.isin(
                segmentation[change_in_context_roi.to_slices()],
                [u, v]
            )
        )

        # get s(u) + s(v)
        lsds_separate = self.lsds[lsds_slice]
        diff = self.target_lsds[lsds_slice] - lsds_separate
        diff[:,not_uv_mask] = 0
        score_separate = np.sum(diff**2)

        # mark u as v in segmentation
        segmentation[segmentation==u] = v

        # get s(u + v)
        lsds_merged = self.lsd_extractor.get_descriptors(
            segmentation,
            roi=change_in_context_roi,
            labels=[v],
            voxel_size=self.voxel_size)
        diff = self.target_lsds[lsds_slice] - lsds_merged
        diff[:,not_uv_mask] = 0
        score_merged = np.sum(diff**2)

        assert lsds_separate.shape == lsds_merged.shape

        logger.debug(
            "Edge score for (%d, %d) is %f - %f = %f",
            u, v, score_merged, score_separate,
            score_merged - score_separate)

        return score_merged - score_separate

    def __get_lsds_edge_rois(self, u, v):
        '''Get two ROIs (change_roi, context_roi).

        change_roi bounds the regions in which LSDs are affected by a merge of
        u and v.

        context_roi is a superset of change_roi and bounds the region that
        needs to be considered to compute LSDs in change_roi.
        '''

        # get node ROIs
        roi_u = self.rag.node[u]['roi']
        roi_v = self.rag.node[v]['roi']

        # the ROI of the complete volume
        total_roi = gp.Roi(
            (0,)*len(self.segmentation.shape),
            self.segmentation.shape)

        # the context used by the shape descriptor in voxels
        context = tuple(int(math.ceil(c/vs)) for c, vs in zip(self.context, self.voxel_size))

        # grow the node ROIs by context and ensure they are still within the
        # total ROI
        roi_u_grown = roi_u.grow(context, context)
        roi_v_grown = roi_v.grow(context, context)
        roi_u_grown = roi_u_grown.intersect(total_roi)
        roi_v_grown = roi_v_grown.intersect(total_roi)

        # LSDs have to be computed and compared to target only within the
        # intersection of the grown node ROIs (other parts of u and v are not
        # affected by the merge, due to finite context)
        change_roi = roi_u_grown.intersect(roi_v_grown)

        # we can further restric the compute ROI to the union of the node ROIs,
        # since voxels outside of the nodes do not contribute, either
        change_roi = change_roi.intersect(roi_u.union(roi_v))

        # the context we need to compute LSDs in change_roi
        context_roi = change_roi.grow(context, context)

        # this can again be limited to the union of the node ROIs
        context_roi = context_roi.intersect(roi_u.union(roi_v))

        return (change_roi, context_roi)

    def __slice_to_roi(self, slices):

        offset = tuple(s.start for s in slices)
        shape = tuple(s.stop - s.start for s in slices)

        roi = gp.Roi(offset, shape)
        roi = roi.snap_to_grid((self.lsd_extractor.downsample,)*roi.dims())

        return roi
