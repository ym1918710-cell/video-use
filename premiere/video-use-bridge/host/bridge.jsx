// Video Use ↔ Premiere Pro bridge (ExtendScript). Called by the panel as
// vu.call(fn, args); every function returns a JSON string.
// eslint-disable
var vu = (function () {
    var TICKS = 254016000000;

    function q(s) { return '"' + String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/\n/g, "\\n").replace(/\r/g, "\\r").replace(/\t/g, "\\t") + '"'; }
    function json(v) {
        if (v === null || v === undefined) return "null";
        if (typeof v === "number") return isFinite(v) ? String(v) : "null";
        if (typeof v === "boolean") return v ? "true" : "false";
        if (typeof v === "string") return q(v);
        if (v instanceof Array) { var a = []; for (var i = 0; i < v.length; i++) a.push(json(v[i])); return "[" + a.join(",") + "]"; }
        var o = []; for (var k in v) if (v.hasOwnProperty(k)) o.push(q(k) + ":" + json(v[k])); return "{" + o.join(",") + "}";
    }
    function seconds(s) { var t = new Time(); t.seconds = s; return t; }
    function ticks(s) { return String(Math.round(s * TICKS)); }

    function walk(item, fn) {
        if (!item) return null;
        for (var i = 0; i < item.children.numItems; i++) {
            var c = item.children[i];
            var hit = fn(c);
            if (hit) return hit;
            if (c.type === ProjectItemType.BIN) { var deep = walk(c, fn); if (deep) return deep; }
        }
        return null;
    }
    function norm(p) { return String(p).replace(/\\/g, "/").toLowerCase(); }
    function findItem(path) {
        var want = norm(path);
        return walk(app.project.rootItem, function (c) {
            try { if (c.type === ProjectItemType.CLIP || c.type === ProjectItemType.FILE) { if (norm(c.getMediaPath()) === want) return c; } } catch (e) {}
            return null;
        });
    }
    function findBin(name) {
        return walk(app.project.rootItem, function (c) { return (c.type === ProjectItemType.BIN && c.name === name) ? c : null; });
    }
    function seqByName(name) {
        for (var i = 0; i < app.project.sequences.numSequences; i++) if (app.project.sequences[i].name === name) return app.project.sequences[i];
        return null;
    }
    function activeSeq(args) { return (args && args.sequence) ? seqByName(args.sequence) : app.project.activeSequence; }
    function trackOf(seq, kind, index) { return kind === "audio" ? seq.audioTracks[index] : seq.videoTracks[index]; }
    function clipAt(track, start) {
        for (var i = 0; i < track.clips.numItems; i++) if (Math.abs(track.clips[i].start.seconds - start) < 0.02) return track.clips[i];
        return null;
    }
    function component(clip, name) {
        for (var i = 0; i < clip.components.numItems; i++) {
            var c = clip.components[i];
            if (c.displayName === name || c.matchName === name) return c;
        }
        return null;
    }
    function prop(comp, name) {
        for (var i = 0; i < comp.properties.numItems; i++) if (comp.properties[i].displayName === name) return comp.properties[i];
        return null;
    }

    function valueOf(v) {
        if (v === null || v === undefined) return null;
        if (typeof v === "number" || typeof v === "string" || typeof v === "boolean") return v;
        if (v instanceof Array) { var a = []; for (var i = 0; i < v.length; i++) a.push(valueOf(v[i])); return a; }
        if (v.seconds !== undefined) return v.seconds;
        try { return String(v); } catch (e) { return null; }
    }
    function readProps(comp, withKeys) {
        var out = [];
        for (var i = 0; i < comp.properties.numItems; i++) {
            var p = comp.properties[i], row = { name: p.displayName };
            try { row.value = valueOf(p.getValue()); } catch (e) {}
            try {
                if (withKeys && p.isTimeVarying()) {
                    var ks = p.getKeys(), keys = [];
                    for (var k = 0; ks && k < ks.length; k++) keys.push({ t: ks[k].seconds, v: valueOf(p.getValueAtKey(ks[k])) });
                    row.keys = keys;
                }
            } catch (e2) {}
            out.push(row);
        }
        return out;
    }
    function describe(clip, withProps) {
        var item = clip.projectItem, row = {
            name: clip.name, start: clip.start.seconds, end: clip.end.seconds, inPoint: clip.inPoint.seconds,
            disabled: clip.disabled === true
        };
        if (item) {
            row.item = { name: item.name, nodeId: item.nodeId, type: item.type };
            try { row.item.path = item.getMediaPath(); } catch (e) {}
            try { row.item.adjustment = item.isAdjustmentLayer(); } catch (e) {}
        }
        try { row.mgt = clip.isMGT(); } catch (e) { row.mgt = false; }
        if (row.mgt) { try { row.mgtParams = readProps(clip.getMGTComponent(), false); } catch (e) {} }
        if (withProps) {
            row.components = [];
            for (var c = 0; c < clip.components.numItems; c++) {
                var comp = clip.components[c];
                row.components.push({ name: comp.displayName, match: comp.matchName, props: readProps(comp, true) });
            }
        }
        return row;
    }
    function findByNode(nodeId) {
        return walk(app.project.rootItem, function (c) { return c.nodeId === nodeId ? c : null; });
    }
    function qeItem(seq, kind, trackIndex, start) {
        app.enableQE();
        var qs = qe.project.getActiveSequence();
        var qt = kind === "audio" ? qs.getAudioTrackAt(trackIndex) : qs.getVideoTrackAt(trackIndex);
        var dom = trackOf(seq, kind, trackIndex), want = -1;
        for (var i = 0; i < dom.clips.numItems; i++) if (Math.abs(dom.clips[i].start.seconds - start) < 0.02) want = i;
        if (want < 0) return null;
        var n = -1;
        for (var j = 0; j < qt.numItems; j++) {
            var it = qt.getItemAt(j);
            if (it && it.type !== "Empty") { n++; if (n === want) return it; }
        }
        return null;
    }

    var api = {
        sequences: function () {
            var out = [];
            for (var i = 0; i < app.project.sequences.numSequences; i++) {
                var s = app.project.sequences[i];
                out.push({ name: s.name, id: s.sequenceID, video: s.videoTracks.numTracks, audio: s.audioTracks.numTracks,
                           end: s.end ? Number(s.end) / TICKS : null });
            }
            return { sequences: out, active: app.project.activeSequence ? app.project.activeSequence.name : null };
        },
        // Everything on a sequence: tracks → clips (item, MOGRT params) and,
        // when `props` is set, every component with its keyframes.
        inspect: function (a) {
            var seq = activeSeq(a);
            if (!seq) return { error: "no sequence " + (a.sequence || "(active)") };
            var s = seq.getSettings(), out = { name: seq.name, width: s.videoFrameWidth, height: s.videoFrameHeight,
                fps: s.videoFrameRate ? 1 / s.videoFrameRate.seconds : null, video: [], audio: [] };
            for (var v = 0; v < seq.videoTracks.numTracks; v++) {
                var vt = seq.videoTracks[v], clips = [];
                for (var i = 0; i < vt.clips.numItems; i++) clips.push(describe(vt.clips[i], a.props));
                out.video.push({ index: v, name: vt.name, clips: clips });
            }
            for (var t = 0; t < seq.audioTracks.numTracks; t++) {
                var at = seq.audioTracks[t], aclips = [];
                for (var j = 0; j < at.clips.numItems; j++) aclips.push(describe(at.clips[j], false));
                out.audio.push({ index: t, name: at.name, clips: aclips });
            }
            return out;
        },
        // Put an existing project item (by nodeId: a MOGRT graphic, an
        // adjustment layer, any clip) on a track at `start` for `duration`.
        placeItem: function (a) {
            var seq = activeSeq(a);
            var item = findByNode(a.nodeId);
            if (!item) return { error: "no project item " + a.nodeId };
            var track = trackOf(seq, a.kind || "video", a.track);
            track.overwriteClip(item, a.start);
            var clip = clipAt(track, a.start);
            if (!clip) return { error: "placed item not found" };
            if (a.duration) clip.end = seconds(a.start + a.duration);
            if (a.name) clip.name = a.name;
            return { ok: true, mgt: clip.isMGT ? clip.isMGT() : false };
        },
        // Set MOGRT text/params on the clip at `start`.
        mgtParams: function (a) {
            var seq = activeSeq(a);
            var clip = clipAt(trackOf(seq, "video", a.track), a.start);
            if (!clip || !clip.isMGT()) return { error: "no MOGRT at " + a.start };
            var mgt = clip.getMGTComponent(), set = [], missing = [];
            for (var name in a.params) {
                if (!a.params.hasOwnProperty(name)) continue;
                var p = null;
                for (var pi = 0; pi < mgt.properties.numItems; pi++) {
                    var cand = mgt.properties[pi];
                    if (cand.displayName !== name) continue;
                    var cv = null; try { cv = cand.getValue(); } catch (e0) {}
                    if (!p) p = cand;
                    if (typeof cv === "string" && cv.indexOf("textEditValue") >= 0) { p = cand; break; }  // the text itself, not its group
                }
                if (!p) { missing.push(name); continue; }
                var cur = p.getValue();
                var val = a.params[name];
                // Text params hold JSON ({"textEditValue": ...}) in recent versions.
                if (typeof cur === "string" && cur.charAt(0) === "{" && typeof val === "string") {
                    try { var o = eval("(" + cur + ")"); o.textEditValue = val; val = json(o); } catch (e) {}
                }
                p.setValue(val, true);
                set.push(name);
            }
            return { ok: true, set: set, missing: missing };
        },
        addEffect: function (a) {
            var seq = activeSeq(a);
            var it = qeItem(seq, a.kind || "video", a.track, a.start);
            if (!it) return { error: "no QE item at " + a.start };
            var fx = (a.kind === "audio") ? qe.project.getAudioEffectByName(a.name) : qe.project.getVideoEffectByName(a.name);
            if (!fx) return { error: "no effect " + a.name };
            if (a.kind === "audio") it.addAudioEffect(fx); else it.addVideoEffect(fx);
            return { ok: true };
        },
        // Keyframes / values on any component property of the clip at `start`.
        setProps: function (a) {
            var seq = activeSeq(a);
            var clip = clipAt(trackOf(seq, a.kind || "video", a.track), a.start);
            if (!clip) return { error: "no clip at " + a.start };
            var comp = component(clip, a.component);
            if (!comp) return { error: "no component " + a.component };
            var done = [];
            for (var name in a.props) {
                if (!a.props.hasOwnProperty(name)) continue;
                var p = prop(comp, name), spec = a.props[name];
                if (!p) continue;
                if (spec.keys) {
                    p.setTimeVarying(true);
                    var base = clip.inPoint.seconds;
                    for (var i = 0; i < spec.keys.length; i++) {
                        var k = spec.keys[i], tt = base + k.t;
                        p.addKey(tt); p.setValueAtKey(tt, k.v, true);
                        if (k.ease && k.ease !== "linear") { try { p.setInterpolationTypeAtKey(tt, 5, true); } catch (e) {} }
                    }
                } else { p.setValue(spec.value, true); }
                done.push(name);
            }
            return { ok: true, set: done };
        },
        // Paste Attributes: every component of a clip in another sequence
        // (effects it lacks are added first), values and keyframes, matched
        // by component order and property index (names repeat, e.g. Blend Mode).
        copyAttributes: function (a) {
            var srcSeq = seqByName(a.fromSequence);
            if (!srcSeq) return { error: "no sequence " + a.fromSequence };
            var src = clipAt(trackOf(srcSeq, a.kind || "video", a.fromTrack), a.fromStart);
            var seq = activeSeq(a);
            var dst = clipAt(trackOf(seq, a.kind || "video", a.track), a.start);
            if (!src || !dst) return { error: "clip missing (src " + !!src + ", dst " + !!dst + ")" };
            var skip = {}; for (var si = 0; a.skip && si < a.skip.length; si++) skip[a.skip[si]] = true;
            var copied = [], added = [], errors = [];
            for (var c = 0; c < src.components.numItems; c++) {
                var sc = src.components[c];
                if (skip[sc.displayName]) continue;
                var dc = null;
                for (var d = 0; d < dst.components.numItems; d++) if (dst.components[d].matchName === sc.matchName) { dc = dst.components[d]; break; }
                if (!dc) {
                    var it = qeItem(seq, a.kind || "video", a.track, a.start);
                    var fx = it ? qe.project.getVideoEffectByName(sc.displayName) : null;
                    if (fx) { it.addVideoEffect(fx); added.push(sc.displayName);
                        dst = clipAt(trackOf(seq, a.kind || "video", a.track), a.start);  // re-read: the DOM sees new effects only on a fresh object
                        for (var d2 = dst.components.numItems - 1; d2 >= 0; d2--) if (dst.components[d2].matchName === sc.matchName || dst.components[d2].displayName === sc.displayName) { dc = dst.components[d2]; break; } }
                }
                if (!dc) continue;
                for (var k = 0; k < sc.properties.numItems && k < dc.properties.numItems; k++) {
                    var sp = sc.properties[k], dp = dc.properties[k];
                    try {
                        if (sp.isTimeVarying() && a.noKeys) {  // settings only: the value at the first key, no animation
                            var k0 = sp.getKeys();
                            if (dp.isTimeVarying()) dp.setTimeVarying(false);
                            dp.setValue(k0 && k0.length ? sp.getValueAtKey(k0[0]) : sp.getValue(), true);
                        } else if (sp.isTimeVarying()) {
                            var keys = sp.getKeys();
                            dp.setTimeVarying(true);
                            for (var q2 = 0; keys && q2 < keys.length; q2++) {
                                var tt = dst.inPoint.seconds + (keys[q2].seconds - src.inPoint.seconds);
                                dp.addKey(tt); dp.setValueAtKey(tt, sp.getValueAtKey(keys[q2]), true);
                            }
                        } else {
                            if (dp.isTimeVarying()) dp.setTimeVarying(false);
                            dp.setValue(sp.getValue(), true);
                        }
                    } catch (e) { errors.push(sc.displayName + "." + sp.displayName + ": " + e); }
                }
                copied.push(sc.displayName);
            }
            return { ok: true, copied: copied, added: added, errors: errors };
        },
        cloneSequence: function (a) {
            var seq = seqByName(a.from);
            if (!seq) return { error: "no sequence " + a.from };
            seq.clone();
            var copy = seqByName(a.from + " Copy");
            if (copy && a.name) copy.name = a.name;
            return { ok: !!copy, name: copy ? copy.name : null };
        },

        ping: function () {
            return { ok: true, version: app.version, project: app.project ? app.project.name : null,
                     path: app.project ? app.project.path : null };
        },
        newProject: function (a) { app.newProject(a.path); return { ok: true, project: app.project.name }; },
        openProject: function (a) { app.openDocument(a.path); return { ok: true, project: app.project.name }; },
        save: function () { app.project.save(); return { ok: true, path: app.project.path }; },

        importFiles: function (a) {
            var bin = findBin(a.bin) || app.project.rootItem.createBin(a.bin);
            var todo = [];
            for (var i = 0; i < a.paths.length; i++) if (!findItem(a.paths[i])) todo.push(a.paths[i]);
            if (todo.length) app.project.importFiles(todo, true, bin, false);
            var missing = [];
            for (var j = 0; j < a.paths.length; j++) if (!findItem(a.paths[j])) missing.push(a.paths[j]);
            return { ok: missing.length === 0, imported: todo.length, missing: missing };
        },

        newSequence: function (a) {
            var old = seqByName(a.name);
            if (old && a.replace) app.project.deleteSequence(old);
            else if (old) return { ok: true, existed: true, name: a.name };
            var item = findItem(a.from);
            if (!item) return { error: "not imported: " + a.from };
            var bin = findBin(a.bin) || app.project.rootItem;
            var seq = app.project.createNewSequenceFromClips(a.name, [item], bin);
            var s = seq.getSettings();
            s.videoFrameWidth = a.width; s.videoFrameHeight = a.height;
            var fr = new Time(); fr.seconds = 1 / a.fps; s.videoFrameRate = fr;
            seq.setSettings(s);
            // createNewSequenceFromClips drops the clip in; start from an empty timeline.
            for (var v = seq.videoTracks.numTracks - 1; v >= 0; v--) for (var k = seq.videoTracks[v].clips.numItems - 1; k >= 0; k--) seq.videoTracks[v].clips[k].remove(false, false);
            for (var t = seq.audioTracks.numTracks - 1; t >= 0; t--) for (var m = seq.audioTracks[t].clips.numItems - 1; m >= 0; m--) seq.audioTracks[t].clips[m].remove(false, false);
            app.project.activeSequence = seq;
            return { ok: true, name: seq.name, video: seq.videoTracks.numTracks, audio: seq.audioTracks.numTracks };
        },

        ensureTracks: function (a) {
            var seq = activeSeq(a);
            app.enableQE();
            var qs = qe.project.getActiveSequence();
            var addV = Math.max(0, a.video - seq.videoTracks.numTracks);
            var addA = Math.max(0, a.audio - seq.audioTracks.numTracks);
            if (addV || addA) qs.addTracks(addV, seq.videoTracks.numTracks, addA, 1, seq.audioTracks.numTracks, 0, 0);
            return { ok: true, video: seq.videoTracks.numTracks, audio: seq.audioTracks.numTracks };
        },

        // Place [in, out) of a file at `start` on one track. Video inserts drop
        // the linked audio unless keepAudio: sound is laid separately.
        place: function (a) {
            var seq = activeSeq(a);
            var item = findItem(a.path);
            if (!item) return { error: "not imported: " + a.path };
            item.setInPoint(a["in"], 4);
            item.setOutPoint(a.out, 4);
            var track = trackOf(seq, a.kind, a.track);
            // Target only the destination track, as an editor does before an
            // overwrite: video never lands on the sound and vice versa.
            var saved = [], v, t2;
            for (v = 0; v < seq.videoTracks.numTracks; v++) { saved.push(seq.videoTracks[v].isTargeted()); seq.videoTracks[v].setTargeted(a.kind !== "audio" && v === a.track, true); }
            for (t2 = 0; t2 < seq.audioTracks.numTracks; t2++) { saved.push(seq.audioTracks[t2].isTargeted()); seq.audioTracks[t2].setTargeted(a.kind === "audio" && t2 === a.track, true); }
            track.overwriteClip(item, a.start);
            var n = 0;
            for (v = 0; v < seq.videoTracks.numTracks; v++) seq.videoTracks[v].setTargeted(saved[n++], true);
            for (t2 = 0; t2 < seq.audioTracks.numTracks; t2++) seq.audioTracks[t2].setTargeted(saved[n++], true);
            item.clearInPoint(4); item.clearOutPoint(4);
            var clip = clipAt(track, a.start);
            if (!clip) return { error: "placed clip not found at " + a.start };
            // An overwrite drags the file's other half along (audio under a
            // picture, picture over a sound). Unlink it and drop that half.
            var other = a.kind === "audio" ? seq.videoTracks : seq.audioTracks, removed = 0;
            for (var ot = 0; ot < other.numTracks; ot++) {
                var stray = clipAt(other[ot], a.start);
                if (stray && stray.projectItem && stray.projectItem.nodeId === item.nodeId &&
                    Math.abs(stray.end.seconds - clip.end.seconds) < 0.05 && !(a.kind !== "audio" && a.keepAudio)) {
                    try {
                        var sel = seq.getSelection(); for (var z = 0; sel && z < sel.length; z++) sel[z].setSelected(false, true);
                        clip.setSelected(true, true); stray.setSelected(true, true);
                        seq.unlinkSelection();
                        clip.setSelected(false, true);
                    } catch (eu) {}
                    stray.remove(false, false); removed++;
                }
            }
            if (a.name) clip.name = a.name;
            return { ok: true, mgt: clip.isMGT ? clip.isMGT() : false };
        },
        // Set MOGRT text/params on the clip at `start`.
        mgtParams: function (a) {
            var seq = activeSeq(a);
            var clip = clipAt(trackOf(seq, "video", a.track), a.start);
            if (!clip || !clip.isMGT()) return { error: "no MOGRT at " + a.start };
            var mgt = clip.getMGTComponent(), set = [], missing = [];
            for (var name in a.params) {
                if (!a.params.hasOwnProperty(name)) continue;
                var p = null;
                for (var pi = 0; pi < mgt.properties.numItems; pi++) {
                    var cand = mgt.properties[pi];
                    if (cand.displayName !== name) continue;
                    var cv = null; try { cv = cand.getValue(); } catch (e0) {}
                    if (!p) p = cand;
                    if (typeof cv === "string" && cv.indexOf("textEditValue") >= 0) { p = cand; break; }  // the text itself, not its group
                }
                if (!p) { missing.push(name); continue; }
                var cur = p.getValue();
                var val = a.params[name];
                // Text params hold JSON ({"textEditValue": ...}) in recent versions.
                if (typeof cur === "string" && cur.charAt(0) === "{" && typeof val === "string") {
                    try { var o = eval("(" + cur + ")"); o.textEditValue = val; val = json(o); } catch (e) {}
                }
                p.setValue(val, true);
                set.push(name);
            }
            return { ok: true, set: set, missing: missing };
        },
        addEffect: function (a) {
            var seq = activeSeq(a);
            var it = qeItem(seq, a.kind || "video", a.track, a.start);
            if (!it) return { error: "no QE item at " + a.start };
            var fx = (a.kind === "audio") ? qe.project.getAudioEffectByName(a.name) : qe.project.getVideoEffectByName(a.name);
            if (!fx) return { error: "no effect " + a.name };
            if (a.kind === "audio") it.addAudioEffect(fx); else it.addVideoEffect(fx);
            return { ok: true };
        },
        // Keyframes / values on any component property of the clip at `start`.
        setProps: function (a) {
            var seq = activeSeq(a);
            var clip = clipAt(trackOf(seq, a.kind || "video", a.track), a.start);
            if (!clip) return { error: "no clip at " + a.start };
            var comp = component(clip, a.component);
            if (!comp) return { error: "no component " + a.component };
            var done = [];
            for (var name in a.props) {
                if (!a.props.hasOwnProperty(name)) continue;
                var p = prop(comp, name), spec = a.props[name];
                if (!p) continue;
                if (spec.keys) {
                    p.setTimeVarying(true);
                    var base = clip.inPoint.seconds;
                    for (var i = 0; i < spec.keys.length; i++) {
                        var k = spec.keys[i], tt = base + k.t;
                        p.addKey(tt); p.setValueAtKey(tt, k.v, true);
                        if (k.ease && k.ease !== "linear") { try { p.setInterpolationTypeAtKey(tt, 5, true); } catch (e) {} }
                    }
                } else { p.setValue(spec.value, true); }
                done.push(name);
            }
            return { ok: true, set: done };
        },
        // Paste Attributes: every component of a clip in another sequence
        // (effects it lacks are added first), values and keyframes, matched
        // by component order and property index (names repeat, e.g. Blend Mode).
        copyAttributes: function (a) {
            var srcSeq = seqByName(a.fromSequence);
            if (!srcSeq) return { error: "no sequence " + a.fromSequence };
            var src = clipAt(trackOf(srcSeq, a.kind || "video", a.fromTrack), a.fromStart);
            var seq = activeSeq(a);
            var dst = clipAt(trackOf(seq, a.kind || "video", a.track), a.start);
            if (!src || !dst) return { error: "clip missing (src " + !!src + ", dst " + !!dst + ")" };
            var skip = {}; for (var si = 0; a.skip && si < a.skip.length; si++) skip[a.skip[si]] = true;
            var copied = [], added = [];
            for (var c = 0; c < src.components.numItems; c++) {
                var sc = src.components[c];
                if (skip[sc.displayName]) continue;
                var dc = null;
                for (var d = 0; d < dst.components.numItems; d++) if (dst.components[d].matchName === sc.matchName) { dc = dst.components[d]; break; }
                if (!dc) {
                    var it = qeItem(seq, a.kind || "video", a.track, a.start);
                    var fx = it ? qe.project.getVideoEffectByName(sc.displayName) : null;
                    if (fx) { it.addVideoEffect(fx); added.push(sc.displayName);
                        for (var d2 = 0; d2 < dst.components.numItems; d2++) if (dst.components[d2].matchName === sc.matchName) dc = dst.components[d2]; }
                }
                if (!dc) continue;
                for (var k = 0; k < sc.properties.numItems && k < dc.properties.numItems; k++) {
                    var sp = sc.properties[k], dp = dc.properties[k];
                    try {
                        if (sp.isTimeVarying() && a.noKeys) {  // settings only: the value at the first key, no animation
                            var k0 = sp.getKeys();
                            if (dp.isTimeVarying()) dp.setTimeVarying(false);
                            dp.setValue(k0 && k0.length ? sp.getValueAtKey(k0[0]) : sp.getValue(), true);
                        } else if (sp.isTimeVarying()) {
                            var keys = sp.getKeys();
                            dp.setTimeVarying(true);
                            for (var q2 = 0; keys && q2 < keys.length; q2++) {
                                var tt = dst.inPoint.seconds + (keys[q2].seconds - src.inPoint.seconds);
                                dp.addKey(tt); dp.setValueAtKey(tt, sp.getValueAtKey(keys[q2]), true);
                            }
                        } else {
                            if (dp.isTimeVarying()) dp.setTimeVarying(false);
                            dp.setValue(sp.getValue(), true);
                        }
                    } catch (e) {}
                }
                copied.push(sc.displayName);
            }
            return { ok: true, copied: copied, added: added };
        },
        cloneSequence: function (a) {
            var seq = seqByName(a.from);
            if (!seq) return { error: "no sequence " + a.from };
            seq.clone();
            var copy = seqByName(a.from + " Copy");
            if (copy && a.name) copy.name = a.name;
            return { ok: !!copy, name: copy ? copy.name : null };
        },

        ping: function () {
            return { ok: true, version: app.version, project: app.project ? app.project.name : null,
                     path: app.project ? app.project.path : null };
        },
        newProject: function (a) { app.newProject(a.path); return { ok: true, project: app.project.name }; },
        openProject: function (a) { app.openDocument(a.path); return { ok: true, project: app.project.name }; },
        save: function () { app.project.save(); return { ok: true, path: app.project.path }; },

        importFiles: function (a) {
            var bin = findBin(a.bin) || app.project.rootItem.createBin(a.bin);
            var todo = [];
            for (var i = 0; i < a.paths.length; i++) if (!findItem(a.paths[i])) todo.push(a.paths[i]);
            if (todo.length) app.project.importFiles(todo, true, bin, false);
            var missing = [];
            for (var j = 0; j < a.paths.length; j++) if (!findItem(a.paths[j])) missing.push(a.paths[j]);
            return { ok: missing.length === 0, imported: todo.length, missing: missing };
        },

        newSequence: function (a) {
            var old = seqByName(a.name);
            if (old && a.replace) app.project.deleteSequence(old);
            else if (old) return { ok: true, existed: true, name: a.name };
            var item = findItem(a.from);
            if (!item) return { error: "not imported: " + a.from };
            var bin = findBin(a.bin) || app.project.rootItem;
            var seq = app.project.createNewSequenceFromClips(a.name, [item], bin);
            var s = seq.getSettings();
            s.videoFrameWidth = a.width; s.videoFrameHeight = a.height;
            var fr = new Time(); fr.seconds = 1 / a.fps; s.videoFrameRate = fr;
            seq.setSettings(s);
            // createNewSequenceFromClips drops the clip in; start from an empty timeline.
            for (var v = seq.videoTracks.numTracks - 1; v >= 0; v--) for (var k = seq.videoTracks[v].clips.numItems - 1; k >= 0; k--) seq.videoTracks[v].clips[k].remove(false, false);
            for (var t = seq.audioTracks.numTracks - 1; t >= 0; t--) for (var m = seq.audioTracks[t].clips.numItems - 1; m >= 0; m--) seq.audioTracks[t].clips[m].remove(false, false);
            app.project.activeSequence = seq;
            return { ok: true, name: seq.name, video: seq.videoTracks.numTracks, audio: seq.audioTracks.numTracks };
        },

        ensureTracks: function (a) {
            var seq = activeSeq(a);
            app.enableQE();
            var qs = qe.project.getActiveSequence();
            var addV = Math.max(0, a.video - seq.videoTracks.numTracks);
            var addA = Math.max(0, a.audio - seq.audioTracks.numTracks);
            if (addV || addA) qs.addTracks(addV, seq.videoTracks.numTracks, addA, 1, seq.audioTracks.numTracks, 0, 0);
            return { ok: true, video: seq.videoTracks.numTracks, audio: seq.audioTracks.numTracks };
        },

        // Place [in, out) of a file at `start` on one track. Video inserts drop
        // the linked audio unless keepAudio: sound is laid separately.
        place: function (a) {
            var seq = activeSeq(a);
            var item = findItem(a.path);
            if (!item) return { error: "not imported: " + a.path };
            item.setInPoint(a["in"], 4);
            item.setOutPoint(a.out, 4);
            var track = trackOf(seq, a.kind, a.track);
            // Target only the destination track, as an editor does before an
            // overwrite: video never lands on the sound and vice versa.
            var saved = [], v, t2;
            for (v = 0; v < seq.videoTracks.numTracks; v++) { saved.push(seq.videoTracks[v].isTargeted()); seq.videoTracks[v].setTargeted(a.kind !== "audio" && v === a.track, true); }
            for (t2 = 0; t2 < seq.audioTracks.numTracks; t2++) { saved.push(seq.audioTracks[t2].isTargeted()); seq.audioTracks[t2].setTargeted(a.kind === "audio" && t2 === a.track, true); }
            track.overwriteClip(item, a.start);
            var n = 0;
            for (v = 0; v < seq.videoTracks.numTracks; v++) seq.videoTracks[v].setTargeted(saved[n++], true);
            for (t2 = 0; t2 < seq.audioTracks.numTracks; t2++) seq.audioTracks[t2].setTargeted(saved[n++], true);
            item.clearInPoint(4); item.clearOutPoint(4);
            var clip = clipAt(track, a.start);
            if (!clip) return { error: "placed clip not found at " + a.start };
            // An overwrite drags the file's other half along (audio under a
            // picture, picture over a sound). Unlink it and drop that half.
            var other = a.kind === "audio" ? seq.videoTracks : seq.audioTracks, removed = 0;
            for (var ot = 0; ot < other.numTracks; ot++) {
                var stray = clipAt(other[ot], a.start);
                if (stray && stray.projectItem && stray.projectItem.nodeId === item.nodeId &&
                    Math.abs(stray.end.seconds - clip.end.seconds) < 0.05 && !(a.kind !== "audio" && a.keepAudio)) {
                    try {
                        var sel = seq.getSelection(); for (var z = 0; sel && z < sel.length; z++) sel[z].setSelected(false, true);
                        clip.setSelected(true, true); stray.setSelected(true, true);
                        seq.unlinkSelection();
                        clip.setSelected(false, true);
                    } catch (eu) {}
                    stray.remove(false, false); removed++;
                }
            }
            if (a.kind === "audio") {
                for (var vv = 0; vv < seq.videoTracks.numTracks; vv++) {
                    var sv = clipAt(seq.videoTracks[vv], a.start);
                    if (sv && sv.projectItem && sv.projectItem.nodeId === item.nodeId && a.kind === "audio" && a.track !== -1 && !a.keepVideo) {
                        // only a stray linked video (never the picture we placed on purpose with the same file)
                        if (Math.abs(sv.end.seconds - clip.end.seconds) < 0.02 && sv.name === item.name && a.removeStrayVideo) sv.remove(false, false);
                    }
                }
            }
            if (a.kind !== "audio" && !a.keepAudio) {
                for (var t = 0; t < seq.audioTracks.numTracks; t++) {
                    var stray = clipAt(seq.audioTracks[t], a.start);
                    if (stray && stray.projectItem && stray.projectItem.nodeId === item.nodeId) stray.remove(false, false);
                }
            }
            if (a.name) clip.name = a.name;
            if (a.disabled) clip.disabled = true;
            return { ok: true, start: clip.start.seconds, end: clip.end.seconds };
        },

        // Motion: anchor/position (normalized), rotation, and scale keyframes.
        motion: function (a) {
            var seq = activeSeq(a);
            var clip = clipAt(trackOf(seq, "video", a.track), a.start);
            if (!clip) return { error: "no clip at " + a.start };
            var m = component(clip, "Motion");
            if (!m) return { error: "no Motion component" };
            if (a.rotation) prop(m, "Rotation").setValue(a.rotation, true);
            if (a.anchor) prop(m, "Anchor Point").setValue(a.anchor, true);
            if (a.position) prop(m, "Position").setValue(a.position, true);
            var scale = prop(m, "Scale");
            if (a.keys && a.keys.length > 1) {
                scale.setTimeVarying(true);
                var base = clip.inPoint.seconds;
                for (var i = 0; i < a.keys.length; i++) {
                    var k = a.keys[i], tt = base + k.t;
                    scale.addKey(tt);
                    scale.setValueAtKey(tt, k.scale, true);
                    if (k.ease && k.ease !== "linear") { try { scale.setInterpolationTypeAtKey(tt, 5, true); } catch (e) {} }
                }
            } else if (a.keys && a.keys.length === 1) {
                scale.setValue(a.keys[0].scale, true);
            }
            return { ok: true };
        },

        volume: function (a) {
            var seq = activeSeq(a);
            var clip = clipAt(trackOf(seq, "audio", a.track), a.start);
            if (!clip) return { error: "no audio clip at " + a.start };
            var v = component(clip, "Volume");
            var level = v && prop(v, "Level");
            if (!level) return { error: "no Volume/Level" };
            level.setValue(Math.pow(10, (a.db - 15) / 20), true);  // Premiere's level scale: 0 dB = 0.1778
            return { ok: true };
        },

        transition: function (a) {
            app.enableQE();
            var qs = qe.project.getActiveSequence();
            var qt = qs.getVideoTrackAt(a.track);
            var seq = activeSeq(a);
            var track = seq.videoTracks[a.track];
            for (var i = 0; i < track.clips.numItems; i++) {
                if (Math.abs(track.clips[i].start.seconds - a.start) < 0.02) {
                    var tr = qe.project.getVideoTransitionByName(a.name || "Cross Dissolve");
                    if (!tr) return { error: "no transition " + a.name };
                    var frames = Math.max(2, Math.round(a.duration * a.fps));
                    qt.getItemAt(i).addTransition(tr, false, String(frames), "0:00", 0.5, true, true);
                    return { ok: true };
                }
            }
            return { error: "no clip at " + a.start };
        },

        captions: function (a) {
            var seq = activeSeq(a);
            var item = findItem(a.srt);
            if (!item) return { error: "srt not imported" };
            seq.createCaptionTrack(item, a.start || 0, Sequence.CAPTION_FORMAT_SUBTITLE);
            return { ok: true };
        },

        mogrt: function (a) {
            var seq = activeSeq(a);
            var clip = seq.importMGT(a.path, ticks(a.start), a.track, a.audioTrack || 0);
            if (!clip) return { error: "importMGT failed: " + a.path };
            clip.end = seconds(a.start + a.duration);
            var set = [], missing = [];
            var mgt = clip.getMGTComponent();
            if (mgt && a.params) {
                for (var name in a.params) {
                    if (!a.params.hasOwnProperty(name)) continue;
                    var p = mgt.properties.getParamForDisplayName(name);
                    if (p) { p.setValue(a.params[name], true); set.push(name); } else missing.push(name);
                }
            }
            return { ok: true, set: set, missing: missing };
        }
    };

    return {
        call: function (fn, args) {
            try {
                if (!api[fn]) return json({ error: "unknown fn " + fn });
                return json(api[fn](args || {}));
            } catch (e) {
                return json({ error: String(e) + (e.line ? " (line " + e.line + ")" : ""), fn: fn });
            }
        }
    };
})();
