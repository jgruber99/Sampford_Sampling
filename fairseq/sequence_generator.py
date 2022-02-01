# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the LICENSE file in
# the root directory of this source tree. An additional grant of patent rights
# can be found in the PATENTS file in the same directory.

import math

import torch

from fairseq import search, utils
from fairseq.models import FairseqIncrementalDecoder


class SequenceGenerator(object):
    def __init__(
        self,
        tgt_dict,
        beam_size=1,
        max_len_a=0,
        max_len_b=200,
        min_len=1,
        stop_early=True,
        normalize_scores=True,
        len_penalty=1.,
        unk_penalty=0.,
        retain_dropout=False,
        sampling=False,
        sampling_topk=-1,
        sampling_temperature=1.,
        diverse_beam_groups=-1,
        diverse_beam_strength=0.5,
        stochastic_beam_search=False,
        naive_stochastic_beam_search=False,
        cps=False,
        nucleus_p=1.,
        match_source_len=False,
        no_repeat_ngram_size=0,
    ):
        """Generates translations of a given source sentence.

        Args:
            tgt_dict (~fairseq.data.Dictionary): target dictionary
            beam_size (int, optional): beam width (default: 1)
            max_len_a/b (int, optional): generate sequences of maximum length
                ax + b, where x is the source length
            min_len (int, optional): the minimum length of the generated output
                (not including end-of-sentence)
            stop_early (bool, optional): stop generation immediately after we
                finalize beam_size hypotheses, even though longer hypotheses
                might have better normalized scores (default: True)
            normalize_scores (bool, optional): normalize scores by the length
                of the output (default: True)
            len_penalty (float, optional): length penalty, where <1.0 favors
                shorter, >1.0 favors longer sentences (default: 1.0)
            unk_penalty (float, optional): unknown word penalty, where <0
                produces more unks, >0 produces fewer (default: 0.0)
            retain_dropout (bool, optional): use dropout when generating
                (default: False)
            sampling (bool, optional): sample outputs instead of beam search
                (default: False)
            sampling_topk (int, optional): only sample among the top-k choices
                at each step (default: -1)
            sampling_temperature (float, optional): temperature for sampling,
                where values >1.0 produces more uniform sampling and values
                <1.0 produces sharper sampling (default: 1.0)
            diverse_beam_groups/strength (float, optional): parameters for
                Diverse Beam Search sampling
            match_source_len (bool, optional): outputs should match the source
                length (default: False)
        """
        self.pad = tgt_dict.pad()
        self.unk = tgt_dict.unk()
        self.eos = tgt_dict.eos()
        self.vocab_size = len(tgt_dict)
        self.beam_size = beam_size
        # the max beam size is the dictionary size - 1, since we never select pad
        self.beam_size = min(beam_size, self.vocab_size - 1)
        self.max_len_a = max_len_a
        self.max_len_b = max_len_b
        self.min_len = min_len
        self.stop_early = stop_early
        self.normalize_scores = normalize_scores
        self.len_penalty = len_penalty
        self.unk_penalty = unk_penalty
        self.retain_dropout = retain_dropout
        self.match_source_len = match_source_len
        self.no_repeat_ngram_size = no_repeat_ngram_size

        assert sampling_topk < 0 or sampling, '--sampling-topk requires --sampling'

        if sampling:
            self.search = search.Sampling(tgt_dict, sampling_topk, sampling_temperature)
        elif diverse_beam_groups > 0:
            self.search = search.DiverseBeamSearch(tgt_dict, diverse_beam_groups, diverse_beam_strength)
        elif match_source_len:
            self.search = search.LengthConstrainedBeamSearch(
                tgt_dict, min_len_a=1, min_len_b=0, max_len_a=1, max_len_b=0,
            )
        elif cps:
            self.search = search.CPS(tgt_dict, sampling_topk, sampling_temperature, nucleus_p)
        else:
            self.search = search.BeamSearch(tgt_dict, naive_stochastic_beam_search, stochastic_beam_search, sampling_topk, sampling_temperature)

    @torch.no_grad()
    def generate(
        self,
        models,
        sample,
        prefix_tokens=None,
        bos_token=None,
        **kwargs
    ):
        """Generate a batch of translations.

        Args:
            models (List[~fairseq.models.FairseqModel]): ensemble of models
            sample (dict): batch
            prefix_tokens (torch.LongTensor, optional): force decoder to begin
                with these tokens
        """
        model = EnsembleModel(models)
        if not self.retain_dropout:
            model.eval()

        # model.forward normally channels prev_output_tokens into the decoder
        # separately, but SequenceGenerator directly calls model.encoder
        encoder_input = {
            k: v for k, v in sample['net_input'].items()
            if k != 'prev_output_tokens'
        }

        src_tokens = encoder_input['src_tokens']
        src_lengths = (src_tokens.ne(self.eos) & src_tokens.ne(self.pad)).long().sum(dim=1)
        input_size = src_tokens.size()
        # batch dimension goes first followed by source lengths
        bsz = input_size[0]
        src_len = input_size[1]
        beam_size = self.beam_size

        if self.match_source_len:
            max_len = src_lengths.max().item()
        else:
            max_len = min(
                int(self.max_len_a * src_len + self.max_len_b),
                # exclude the EOS marker
                model.max_decoder_positions() - 1,
            )

        # compute the encoder output for each beam
        encoder_outs = model.forward_encoder(encoder_input)
        new_order = torch.arange(bsz).view(-1, 1).repeat(1, beam_size).view(-1)
        new_order = new_order.to(src_tokens.device).long()
        encoder_outs = model.reorder_encoder_out(encoder_outs, new_order)

        # initialize buffers
        scores = src_tokens.new(bsz * beam_size, max_len + 1).float().fill_(0)
        scores_buf = scores.clone()
        log_ps = src_tokens.data.new(bsz * beam_size, max_len + 1).float().fill_(0)
        log_ps_buf = log_ps.clone()
        log_ps_t = src_tokens.data.new(bsz * beam_size, max_len + 1).float().fill_(0)
        log_ps_t_buf = log_ps_t.clone()
        tokens = src_tokens.data.new(bsz * beam_size, max_len + 2).long().fill_(self.pad)
        tokens_buf = tokens.clone()
        tokens[:, 0] = bos_token or self.eos
        attn, attn_buf = None, None
        nonpad_idxs = None

        # list of completed sentences
        finalized = [[] for i in range(bsz)]
        finished = [False for i in range(bsz)]
        worst_finalized = [{'idx': None, 'score': -math.inf} for i in range(bsz)]
        num_remaining_sent = bsz

        # number of candidate hypos per step
        cand_size = 2 * beam_size  # 2 x beam size in case half are EOS

        # offset arrays for converting between different indexing schemes
        bbsz_offsets = (torch.arange(0, bsz) * beam_size).unsqueeze(1).type_as(tokens)
        cand_offsets = torch.arange(0, cand_size).type_as(tokens)

        # helper function for allocating buffers on the fly
        buffers = {}

        def buffer(name, type_of=tokens):  # noqa
            if name not in buffers:
                buffers[name] = type_of.new()
            return buffers[name]

        def is_finished(sent, step, unfinalized_scores=None, unfin_idx=None):
            """
            Check whether we've finished generation for a given sentence, by
            comparing the worst score among finalized hypotheses to the best
            possible score among unfinalized hypotheses.
            """
            assert len(finalized[sent]) <= beam_size
            if len(finalized[sent]) == beam_size:
                if self.stop_early or step == max_len or unfinalized_scores is None:
                    return True
                # stop if the best unfinalized score is worse than the worst
                # finalized one
                # WK: Bugfix, see https://github.com/pytorch/fairseq/commit/fa52d20277246a59c13260fdfb7d71101e521478
                best_unfinalized_score = unfinalized_scores[sent if unfin_idx is None else unfin_idx].max()
                if self.normalize_scores:
                    assert False, "TODO find out if not early stopping with normalized scores is valid!"
                    # TODO: shouldn't we normalize by current length rather than max length?
                    best_unfinalized_score /= max_len ** self.len_penalty
                if worst_finalized[sent]['score'] >= best_unfinalized_score:
                    return True
            return False

        def finalize_hypos(step, bbsz_idx, eos_scores, eos_log_ps, eos_log_ps_t, unfinalized_scores=None):
            """
            Finalize the given hypotheses at this step, while keeping the total
            number of finalized hypotheses per sentence <= beam_size.

            Note: the input must be in the desired finalization order, so that
            hypotheses that appear earlier in the input are preferred to those
            that appear later.

            Args:
                step: current time step
                bbsz_idx: A vector of indices in the range [0, bsz*beam_size),
                    indicating which hypotheses to finalize
                eos_scores: A vector of the same size as bbsz_idx containing
                    scores for each hypothesis
                unfinalized_scores: A vector containing scores for all
                    unfinalized hypotheses
            """
            assert bbsz_idx.numel() == eos_scores.numel()

            # clone relevant token and attention tensors
            tokens_clone = tokens.index_select(0, bbsz_idx)
            tokens_clone = tokens_clone[:, 1:step + 2]  # skip the first index, which is EOS
            tokens_clone[:, step] = self.eos
            attn_clone = attn.index_select(0, bbsz_idx)[:, :, 1:step+2] if attn is not None else None

            # compute scores per token position
            pos_scores = scores.index_select(0, bbsz_idx)[:, :step+1]
            pos_scores[:, step] = eos_scores
            # convert from cumulative to per-position scores
            pos_scores[:, 1:] = pos_scores[:, 1:] - pos_scores[:, :-1]

            assert not torch.isnan(pos_scores).any(), "Nans in scores"

            # compute log_p per token position (without temperature)
            pos_log_ps = log_ps.index_select(0, bbsz_idx)[:, :step + 1]
            pos_log_ps[:, step] = eos_log_ps
            # convert from cumulative to per-position scores
            pos_log_ps[:, 1:] = pos_log_ps[:, 1:] - pos_log_ps[:, :-1]

            # compute log_p per token position (with temperature)
            pos_log_ps_t = log_ps_t.index_select(0, bbsz_idx)[:, :step + 1]
            pos_log_ps_t[:, step] = eos_log_ps_t
            # convert from cumulative to per-position scores
            pos_log_ps_t[:, 1:] = pos_log_ps_t[:, 1:] - pos_log_ps_t[:, :-1]

            # normalize sentence-level scores
            if self.normalize_scores:
                eos_scores /= (step + 1) ** self.len_penalty

            cum_unfin = []
            prev = 0
            for f in finished:
                if f:
                    prev += 1
                else:
                    cum_unfin.append(prev)

            sents_seen = set()
            for i, (idx, score, log_p, log_p_t) in enumerate(
                    zip(bbsz_idx.tolist(), eos_scores.tolist(), eos_log_ps.tolist(), eos_log_ps_t.tolist())):
                unfin_idx = idx // beam_size
                sent = unfin_idx + cum_unfin[unfin_idx]

                sents_seen.add((sent, unfin_idx))

                if self.match_source_len and step > src_lengths[unfin_idx]:
                    score = -math.inf

                def get_hypo():

                    if attn_clone is not None:
                        # remove padding tokens from attn scores
                        hypo_attn = attn_clone[i][nonpad_idxs[sent]]
                        _, alignment = hypo_attn.max(dim=0)
                    else:
                        hypo_attn = None
                        alignment = None

                    return {
                        'tokens': tokens_clone[i],
                        'score': score,
                        'log_p': log_p,
                        'log_p_t': log_p_t,
                        'attention': hypo_attn,  # src_len x tgt_len
                        'alignment': alignment,
                        'positional_scores': pos_scores[i],
                        'positional_log_ps': pos_log_ps[i],
                        'positional_log_ps_t': pos_log_ps_t[i],
                    }

                if len(finalized[sent]) < beam_size:
                    new_hypo = get_hypo()
                    finalized[sent].append(new_hypo)
                elif not self.stop_early and score > worst_finalized[sent]['score']:
                    # replace worst hypo for this sentence with new/better one
                    worst_idx = worst_finalized[sent]['idx']
                    if worst_idx is not None:
                        finalized[sent][worst_idx] = get_hypo()

                    # find new worst finalized hypo for this sentence
                    idx, s = min(enumerate(finalized[sent]), key=lambda r: r[1]['score'])
                    worst_finalized[sent] = {
                        'score': s['score'],
                        'idx': idx,
                    }

            newly_finished = []
            for sent, unfin_idx in sents_seen:
                # check termination conditions for this sentence
                if not finished[sent] and is_finished(sent, step, unfinalized_scores, unfin_idx=unfin_idx):
                    finished[sent] = True
                    newly_finished.append(unfin_idx)
            return newly_finished

        reorder_state = None
        batch_idxs = None
        for step in range(max_len + 1):  # one extra step for EOS marker
            # reorder decoder internal states based on the prev choice of beams
            if reorder_state is not None:
                if batch_idxs is not None:
                    # update beam indices to take into account removed sentences
                    corr = batch_idxs - torch.arange(batch_idxs.numel()).type_as(batch_idxs)
                    reorder_state.view(-1, beam_size).add_(corr.unsqueeze(-1) * beam_size)
                model.reorder_incremental_state(reorder_state)
                model.reorder_encoder_out(encoder_outs, reorder_state)

            lprobs, avg_attn_scores = model.forward_decoder(tokens[:, :step + 1], encoder_outs)

            lprobs[:, self.pad] = -math.inf  # never select pad
            lprobs[:, self.unk] -= self.unk_penalty  # apply unk penalty

            if self.no_repeat_ngram_size > 0:
                # for each beam and batch sentence, generate a list of previous ngrams
                gen_ngrams = [{} for bbsz_idx in range(bsz * beam_size)]
                for bbsz_idx in range(bsz * beam_size):
                    gen_tokens = tokens[bbsz_idx].tolist()
                    for ngram in zip(*[gen_tokens[i:] for i in range(self.no_repeat_ngram_size)]):
                        gen_ngrams[bbsz_idx][tuple(ngram[:-1])] = \
                                gen_ngrams[bbsz_idx].get(tuple(ngram[:-1]), []) + [ngram[-1]]

            # Record attention scores
            if avg_attn_scores is not None:
                if attn is None:
                    attn = scores.new(bsz * beam_size, src_tokens.size(1), max_len + 2)
                    attn_buf = attn.clone()
                    nonpad_idxs = src_tokens.ne(self.pad)
                attn[:, :, step + 1].copy_(avg_attn_scores)

            scores = scores.type_as(lprobs)
            log_ps = log_ps.type_as(lprobs)
            log_ps_t = log_ps_t.type_as(lprobs)
            scores_buf = scores_buf.type_as(lprobs)
            eos_bbsz_idx = buffer('eos_bbsz_idx')
            eos_scores = buffer('eos_scores', type_of=scores)
            eos_log_ps = buffer('eos_log_ps', type_of=scores)
            eos_log_ps_t = buffer('eos_log_ps_t', type_of=scores)
            if step < max_len:
                print("{}/{}".format(step, max_len))
                self.search.set_src_lengths(src_lengths)

                if self.no_repeat_ngram_size > 0:
                    def calculate_banned_tokens(bbsz_idx):
                        # before decoding the next token, prevent decoding of ngrams that have already appeared
                        ngram_index = tuple(tokens[bbsz_idx, step + 2 - self.no_repeat_ngram_size:step + 1].tolist())
                        return gen_ngrams[bbsz_idx].get(ngram_index, [])

                    if step + 2 - self.no_repeat_ngram_size >= 0:
                        # no banned tokens if we haven't generated no_repeat_ngram_size tokens yet
                        banned_tokens = [calculate_banned_tokens(bbsz_idx) for bbsz_idx in range(bsz * beam_size)]
                    else:
                        banned_tokens = [[] for bbsz_idx in range(bsz * beam_size)]

                    for bbsz_idx in range(bsz * beam_size):
                        lprobs[bbsz_idx, banned_tokens[bbsz_idx]] = -math.inf

                if prefix_tokens is not None and step < prefix_tokens.size(1):
                    assert False, "Prefix tokens not yet supported"
                    probs_slice = lprobs.view(bsz, -1, lprobs.size(-1))[:, 0, :]
                    cand_scores = torch.gather(
                        probs_slice, dim=1,
                        index=prefix_tokens[:, step].view(-1, 1)
                    ).view(-1, 1).repeat(1, cand_size)
                    if step > 0:
                        # save cumulative scores for each hypothesis
                        cand_scores.add_(scores[:, step - 1].view(bsz, beam_size).repeat(1, 2))
                    cand_indices = prefix_tokens[:, step].view(-1, 1).repeat(1, cand_size)
                    cand_beams = torch.zeros_like(cand_indices)

                    # handle prefixes of different lengths
                    partial_prefix_mask = prefix_tokens[:, step].eq(self.pad)
                    if partial_prefix_mask.any():
                        partial_scores, partial_indices, partial_beams = self.search.step(
                            step,
                            lprobs.view(bsz, -1, self.vocab_size),
                            scores.view(bsz, beam_size, -1)[:, :, :step],
                        )
                        cand_scores[partial_prefix_mask] = partial_scores[partial_prefix_mask]
                        cand_indices[partial_prefix_mask] = partial_indices[partial_prefix_mask]
                        cand_beams[partial_prefix_mask] = partial_beams[partial_prefix_mask]
                else:
                    cand_scores, cand_log_p, cand_log_p_t, cand_indices, cand_beams = self.search.step(
                        step,
                        lprobs.view(bsz, -1, self.vocab_size),
                        scores.view(bsz, beam_size, -1)[:, :, :step],
                        log_ps.view(bsz, beam_size, -1)[:, :, :step],
                        log_ps_t.view(bsz, beam_size, -1)[:, :, :step]
                    )
            else:
                # Note: we have one flat batch of bsz * beam_size, by sorting we mix everything
                # However, we will pass along the indices of the sort, from which the id of the sentence
                # can be recovered (by computing index % bsz), and this is used in the finalize_hypos
                # this may seem a bit inefficient, comparing to reshaping and then sorting for each sentence,
                # but this way we can deal with varying numbers of samples per sequence easily
                if ((isinstance(self.search, search.BeamSearch)) and self.search.stochastic) or isinstance(self.search, search.CPS):
                    # For beam search, we simply stop here with the k samples we have
                    # Sort by the perturbed log-probability
                    torch.sort(
                        scores[:, step - 1],
                        descending=True,
                        out=(eos_scores, eos_bbsz_idx),
                    )
                else:
                    # All other cases (beam search, sampling)
                    # make probs contain cumulative scores for each hypothesis
                    lprobs.add_(scores[:, step - 1].unsqueeze(-1))

                    # finalize all active hypotheses once we hit max_len
                    # pick the hypothesis with the highest prob of EOS right now
                    torch.sort(
                            lprobs[:, self.eos],
                            descending=True,
                            out=(eos_scores, eos_bbsz_idx),
                        )

                # We do not want lprobs as scores since the last probs have been added,
                # this is not what is actually what happens if we sample eos with prob 1
                torch.gather(scores[:, step - 1], -1, eos_bbsz_idx, out=eos_scores)
                torch.gather(log_ps[:, step - 1], -1, eos_bbsz_idx, out=eos_log_ps)
                torch.gather(log_ps_t[:, step - 1], -1, eos_bbsz_idx, out=eos_log_ps_t)
                # Sort without dimension sorts on last dimension
                num_remaining_sent -= len(finalize_hypos(step, eos_bbsz_idx, eos_scores, eos_log_ps, eos_log_ps_t))
                assert num_remaining_sent == 0
                break

            # cand_bbsz_idx contains beam indices for the top candidate
            # hypotheses, with a range of values: [0, bsz*beam_size),
            # and dimensions: [bsz, cand_size]
            cand_bbsz_idx = cand_beams.add(bbsz_offsets)

            # finalize hypotheses that end in eos
            eos_mask = cand_indices.eq(self.eos)

            finalized_sents = set()
            if step >= self.min_len:
                # only consider eos when it's among the top beam_size indices
                torch.masked_select(
                    cand_bbsz_idx[:, :beam_size],
                    mask=eos_mask[:, :beam_size],
                    out=eos_bbsz_idx,
                )
                if eos_bbsz_idx.numel() > 0:
                    torch.masked_select(
                        cand_scores[:, :beam_size],
                        mask=eos_mask[:, :beam_size],
                        out=eos_scores,
                    )
                    torch.masked_select(
                        cand_log_p[:, :beam_size],
                        mask=eos_mask[:, :beam_size],
                        out=eos_log_ps,
                    )
                    torch.masked_select(
                        cand_log_p_t[:, :beam_size],
                        mask=eos_mask[:, :beam_size],
                        out=eos_log_ps_t,
                    )
                    finalized_sents = finalize_hypos(
                        step, eos_bbsz_idx, eos_scores, eos_log_ps, eos_log_ps_t, cand_scores
                    )
                    num_remaining_sent -= len(finalized_sents)

            assert num_remaining_sent >= 0
            if num_remaining_sent == 0:
                break
            assert step < max_len

            if len(finalized_sents) > 0:
                new_bsz = bsz - len(finalized_sents)

                # construct batch_idxs which holds indices of batches to keep for the next pass
                batch_mask = cand_indices.new_ones(bsz)
                batch_mask[cand_indices.new(finalized_sents)] = 0
                batch_idxs = batch_mask.nonzero().squeeze(-1)

                eos_mask = eos_mask[batch_idxs]
                cand_beams = cand_beams[batch_idxs]
                bbsz_offsets.resize_(new_bsz, 1)
                cand_bbsz_idx = cand_beams.add(bbsz_offsets)
                cand_scores = cand_scores[batch_idxs]
                cand_log_p = cand_log_p[batch_idxs]
                cand_log_p_t = cand_log_p_t[batch_idxs]
                cand_indices = cand_indices[batch_idxs]
                if prefix_tokens is not None:
                    prefix_tokens = prefix_tokens[batch_idxs]
                src_lengths = src_lengths[batch_idxs]

                scores = scores.view(bsz, -1)[batch_idxs].view(new_bsz * beam_size, -1)
                scores_buf.resize_as_(scores)
                log_ps = log_ps.view(bsz, -1)[batch_idxs].view(new_bsz * beam_size, -1)
                log_ps_buf.resize_as_(log_ps)
                log_ps_t = log_ps_t.view(bsz, -1)[batch_idxs].view(new_bsz * beam_size, -1)
                log_ps_t_buf.resize_as_(log_ps_t)
                tokens = tokens.view(bsz, -1)[batch_idxs].view(new_bsz * beam_size, -1)
                tokens_buf.resize_as_(tokens)
                if attn is not None:
                    attn = attn.view(bsz, -1)[batch_idxs].view(new_bsz * beam_size, attn.size(1), -1)
                    attn_buf.resize_as_(attn)
                bsz = new_bsz
            else:
                batch_idxs = None

            # set active_mask so that values > cand_size indicate eos hypos
            # and values < cand_size indicate candidate active hypos.
            # After, the min values per row are the top candidate active hypos
            active_mask = buffer('active_mask')
            torch.add(
                eos_mask.type_as(cand_offsets) * cand_size,
                cand_offsets[:eos_mask.size(1)],
                out=active_mask,
            )
            # get the top beam_size active hypotheses, which are just the hypos
            # with the smallest values in active_mask
            active_hypos, _ignore = buffer('active_hypos'), buffer('_ignore')
            torch.topk(
                active_mask, k=beam_size, dim=1, largest=False,
                out=(_ignore, active_hypos)
            )

            active_bbsz_idx = buffer('active_bbsz_idx')
            torch.gather(
                cand_bbsz_idx, dim=1, index=active_hypos,
                out=active_bbsz_idx,
            )
            active_scores = torch.gather(
                cand_scores, dim=1, index=active_hypos,
                out=scores[:, step].view(bsz, beam_size),
            )

            active_bbsz_idx = active_bbsz_idx.view(-1)
            active_scores = active_scores.view(-1)

            # copy tokens and scores for active hypotheses
            torch.index_select(
                tokens[:, :step + 1], dim=0, index=active_bbsz_idx,
                out=tokens_buf[:, :step + 1],
            )
            torch.gather(
                cand_indices, dim=1, index=active_hypos,
                out=tokens_buf.view(bsz, beam_size, -1)[:, :, step + 1],
            )
            if step > 0:
                torch.index_select(
                    scores[:, :step], dim=0, index=active_bbsz_idx,
                    out=scores_buf[:, :step],
                )
                torch.index_select(
                    log_ps[:, :step], dim=0, index=active_bbsz_idx,
                    out=log_ps_buf[:, :step],
                )
                torch.index_select(
                    log_ps_t[:, :step], dim=0, index=active_bbsz_idx,
                    out=log_ps_t_buf[:, :step],
                )
            torch.gather(
                cand_scores, dim=1, index=active_hypos,
                out=scores_buf.view(bsz, beam_size, -1)[:, :, step],
            )
            torch.gather(
                cand_log_p, dim=1, index=active_hypos,
                out=log_ps_buf.view(bsz, beam_size, -1)[:, :, step],
            )
            torch.gather(
                cand_log_p_t, dim=1, index=active_hypos,
                out=log_ps_t_buf.view(bsz, beam_size, -1)[:, :, step],
            )

            # copy attention for active hypotheses
            if attn is not None:
                torch.index_select(
                    attn[:, :, :step + 2], dim=0, index=active_bbsz_idx,
                    out=attn_buf[:, :, :step + 2],
                )

            # swap buffers
            tokens, tokens_buf = tokens_buf, tokens
            scores, scores_buf = scores_buf, scores
            log_ps, log_ps_buf = log_ps_buf, log_ps
            log_ps_t, log_ps_t_buf = log_ps_t_buf, log_ps_t
            if attn is not None:
                attn, attn_buf = attn_buf, attn

            # reorder incremental state in decoder
            reorder_state = active_bbsz_idx

        # sort by score descending
        for sent in range(len(finalized)):
            finalized[sent] = sorted(finalized[sent], key=lambda r: r['score'], reverse=True)

        return finalized


class EnsembleModel(torch.nn.Module):
    """A wrapper around an ensemble of models."""

    def __init__(self, models):
        super().__init__()
        self.models = torch.nn.ModuleList(models)
        self.incremental_states = None
        if all(isinstance(m.decoder, FairseqIncrementalDecoder) for m in models):
            self.incremental_states = {m: {} for m in models}

    def has_encoder(self):
        return hasattr(self.models[0], 'encoder')

    def max_decoder_positions(self):
        return min(m.max_decoder_positions() for m in self.models)

    @torch.no_grad()
    def forward_encoder(self, encoder_input):
        if not self.has_encoder():
            return None
        return [model.encoder(**encoder_input) for model in self.models]

    @torch.no_grad()
    def forward_decoder(self, tokens, encoder_outs):
        if len(self.models) == 1:
            return self._decode_one(
                tokens,
                self.models[0],
                encoder_outs[0] if self.has_encoder() else None,
                self.incremental_states,
                log_probs=True,
            )

        log_probs = []
        avg_attn = None
        for model, encoder_out in zip(self.models, encoder_outs):
            probs, attn = self._decode_one(tokens, model, encoder_out, self.incremental_states, log_probs=True)
            log_probs.append(probs)
            if attn is not None:
                if avg_attn is None:
                    avg_attn = attn
                else:
                    avg_attn.add_(attn)
        avg_probs = torch.logsumexp(torch.stack(log_probs, dim=0), dim=0) - math.log(len(self.models))
        if avg_attn is not None:
            avg_attn.div_(len(self.models))
        return avg_probs, avg_attn

    def _decode_one(self, tokens, model, encoder_out, incremental_states, log_probs):
        if self.incremental_states is not None:
            decoder_out = list(model.decoder(tokens, encoder_out, incremental_state=self.incremental_states[model]))
        else:
            decoder_out = list(model.decoder(tokens, encoder_out))
        decoder_out[0] = decoder_out[0][:, -1:, :]
        attn = decoder_out[1]
        if type(attn) is dict:
            attn = attn['attn']
        if attn is not None:
            if type(attn) is dict:
                attn = attn['attn']
            attn = attn[:, -1, :]
        probs = model.get_normalized_probs(decoder_out, log_probs=log_probs)
        probs = probs[:, -1, :]
        return probs, attn

    def reorder_encoder_out(self, encoder_outs, new_order):
        if not self.has_encoder():
            return
        return [
            model.encoder.reorder_encoder_out(encoder_out, new_order)
            for model, encoder_out in zip(self.models, encoder_outs)
        ]

    def reorder_incremental_state(self, new_order):
        if self.incremental_states is None:
            return
        for model in self.models:
            model.decoder.reorder_incremental_state(self.incremental_states[model], new_order)