from torch import nn
from torch.nn import init
import torch.nn.functional as F

from core.config import cfg
from modeling.generate_anchors import generate_anchors
from modeling.generate_proposals import GenerateProposalsOp
from modeling.generate_proposal_labels import GenerateProposalLabelsOp
import utils.net as net_utils


class Single_Scale_RPN_Outputs(nn.Module):
    def __init__(self, dim_in, spatial_scale):
        super().__init__()
        self.dim_in = dim_in
        self.dim_out = dim_in if cfg.RPN.OUT_DIM_AS_IN_DIM else cfg.RPN.OUT_DIM
        anchors = generate_anchors(
            stride=1. / spatial_scale,
            sizes=cfg.RPN.SIZES,
            aspect_ratios=cfg.RPN.ASPECT_RATIOS)
        num_anchors = anchors.shape[0]

        # RPN hidden representation
        self.RPN_conv = nn.Conv2d(self.dim_in, self.dim_out, 3, 1, 1)
        # Proposal classification scores
        self.n_score_out = num_anchors * 2 if cfg.RPN.CLS_ACTIVATION == 'softmax' \
            else num_anchors
        self.RPN_cls_score = nn.Conv2d(self.dim_out, self.n_score_out, 1, 1, 0)
        # Proposal bbox regression deltas
        self.RPN_bbox_pred = nn.Conv2d(self.dim_out, num_anchors * 4, 1, 1, 0)

        self.RPN_GenerateProposals = GenerateProposalsOp(anchors, spatial_scale)
        self.RPN_GenerateProposalLabels = GenerateProposalLabelsOp()

        self._init_weights()

    def _init_weights(self):
        init.normal(self.RPN_conv.weight, std=0.01)
        init.constant(self.RPN_conv.bias, 0)
        init.normal(self.RPN_cls_score.weight, std=0.01)
        init.constant(self.RPN_cls_score.bias, 0)
        init.normal(self.RPN_bbox_pred.weight, std=0.01)
        init.constant(self.RPN_bbox_pred.bias, 0)

    def forward(self, x, im_info, roidb=None):
        """
        x: feature maps from the backbone network. (Variable)
        im_info: (CPU Variable)
        roidb: (list of ndarray)
        """
        rpn_conv = F.relu(self.RPN_conv(x), inplace=True)

        rpn_cls_logits = self.RPN_cls_score(rpn_conv)

        rpn_bbox_pred = self.RPN_bbox_pred(rpn_conv)

        return_dict = {
            'rpn_cls_logits': rpn_cls_logits, 'rpn_bbox_pred': rpn_bbox_pred}

        if not self.training or cfg.MODEL.FASTER_RCNN:
            # Proposals are needed during:
            #  1) inference (== not model.train) for RPN only and Faster R-CNN
            #  OR
            #  2) training for Faster R-CNN
            # Otherwise (== training for RPN only), proposals are not needed
            if cfg.RPN.CLS_ACTIVATION == 'softmax':
                B, C, H, W = rpn_cls_logits.size()
                rpn_cls_prob = F.softmax(
                    rpn_cls_logits.view(B, 2, C / 2, H, W), dim=1).view(
                        B, C, H, W)
                rpn_cls_prob = rpn_bbox_pred[:, 1].view(B, C / 2, H, W)
            else:
                rpn_cls_prob = F.sigmoid(rpn_cls_logits)

            rpn_rois, rpn_rois_prob = self.RPN_GenerateProposals(
                rpn_cls_prob, rpn_bbox_pred, im_info)

            return_dict['rpn_rois'] = rpn_rois
            return_dict['rpn_rois_prob'] = rpn_rois_prob

        if cfg.MODEL.FASTER_RCNN :
            if self.training:
                # Add op that generates training labels for in-network RPN proposals
                blobs_out = self.RPN_GenerateProposalLabels(rpn_rois, roidb, im_info)
                return_dict.update(blobs_out)
            else:
                # Alias rois to rpn_rois for inference
                return_dict['rois'] = return_dict['rpn_rois']

        return return_dict


def single_scale_rpn_losses(
        rpn_cls_logits, rpn_bbox_pred,
        rpn_labels_int32_wide, rpn_bbox_targets_wide,
        rpn_bbox_inside_weights_wide, rpn_bbox_outside_weights_wide):
    h, w = rpn_cls_logits.shape[2:]
    rpn_labels_int32 = rpn_labels_int32_wide[:, :, :h, :w]   # -1 means ignore
    h, w = rpn_bbox_pred.shape[2:]
    rpn_bbox_targets = rpn_bbox_targets_wide[:, :, :h, :w]
    rpn_bbox_inside_weights = rpn_bbox_inside_weights_wide[:, :, :h, :w]
    rpn_bbox_outside_weights = rpn_bbox_outside_weights_wide[:, :, :h, :w]

    if cfg.RPN.CLS_ACTIVATION == 'softmax':
        B, C, H, W = rpn_cls_logits.size()
        rpn_cls_logits = rpn_cls_logits.view(B, 2, C / 2, H, W).permute(0, 2, 3, 4, 1).view(-1, 2)
        rpn_labels_int32 = rpn_labels_int32.view(-1).long()
        loss_rpn_cls = F.cross_entropy(rpn_cls_logits, rpn_labels_int32, ignore_index=-1)
    else:
        weight = (rpn_labels_int32 != -1).float()
        loss_rpn_cls = F.binary_cross_entropy_with_logits(
            rpn_cls_logits, rpn_labels_int32.float(), weight)

    loss_rpn_bbox = net_utils.smooth_l1_loss(
        rpn_bbox_pred, rpn_bbox_targets, rpn_bbox_inside_weights, rpn_bbox_outside_weights,
        beta=1/9)

    return loss_rpn_cls, loss_rpn_bbox
