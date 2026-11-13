"""Render bookkeeping tests: the per-shot cache key, draft quality, and what publish leaves on disk.

    python tests/render_test.py        (needs numpy and pillow; the publish test also needs ffmpeg and is skipped without it)

Everything happens in a temp folder with CUTVELVET_HOME pointed at another, and agent starts are switched off.
"""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ['CUTVELVET_HOME'] = tempfile.mkdtemp(prefix='cutvelvet-render-home-')
os.environ['CUTVELVET_AGENT_DISABLED'] = '1'          # nothing here starts an agent; make sure nothing can

from cutvelvet import cli, engine, settings as SETTINGS, timeline as TL  # noqa: E402

FPS = 24
PROJ = {'fps': FPS, 'canvas': [1920, 1080]}


def panel(t0, t1, **kw):
    return dict({'src': 'footage/a.mp4', 'ss': 0, 't0': t0, 't1': t1, 'rect': [0, 0, 960, 540], 'region': [0.5, 0.5, 0.5]}, **kw)


def timeline(b_start=21.87, a_end=21.875):
    return {'id': 'demo', 'title': 'Demo', 'duration': 30.0, 'shots': [
        {'id': 'A', 'label': 'a', 't0': 0, 't1': a_end, 'texts': [], 'fx': {}, 'panels': [panel(0, a_end)]},
        {'id': 'B', 'label': 'b', 't0': a_end, 't1': 30.0, 'texts': [], 'fx': {}, 'panels': [panel(b_start, 30.0, ss=40)]}]}


class CacheKey(unittest.TestCase):
    def key(self, tl, sid):
        return TL.shot_key(tl, TL.get_shot(tl, sid), PROJ, 0.5)

    def test_frames_a_panel_is_drawn_on(self):
        # 21.875 s is frame 525. A panel starting at 21.87 is first drawn on frame 525 (21.875), not 524 (21.8333).
        self.assertFalse(TL.on_frames(panel(21.87, 30), 0, 525, FPS))
        self.assertTrue(TL.on_frames(panel(21.87, 30), 525, 720, FPS))
        self.assertTrue(TL.on_frames(panel(21.83, 30), 0, 525, FPS))                # 21.83 <= 21.8333: it is on frame 524
        self.assertFalse(TL.on_frames(panel(0, 21.8333), 524, 525, FPS))            # ends before frame 524's instant (21.83333...)
        self.assertTrue(TL.on_frames(panel(0, 21.84), 524, 525, FPS))
        self.assertFalse(TL.on_frames(panel(5.01, 5.02), 0, 720, FPS))              # lives between frames 120 and 121: never drawn
        self.assertTrue(TL.on_frames(panel(5.0, 5.01), 0, 720, FPS))                # starts exactly on frame 120
        for t0, t1 in ((0, 1), (0.5, 0.52), (3.999, 4.0), (4.0, 4.0417), (10.01, 10.02)):
            p = panel(t0, t1)
            want = any(p['t0'] <= n / FPS < p['t1'] for n in range(0, 720))          # the renderer's own rule, brute force
            self.assertEqual(TL.on_frames(p, 0, 720, FPS), want, (t0, t1))

    def test_changing_one_shot_rerenders_one_shot_even_with_an_off_grid_cut(self):
        tl = timeline(b_start=21.87)
        before = {s: self.key(tl, s) for s in 'AB'}
        changed = copy.deepcopy(tl)
        changed['shots'][1]['panels'][0]['expo'] = 1.5                               # edit shot B only
        self.assertEqual(self.key(changed, 'A'), before['A'], 'shot A was invalidated by a change that is not on any of its frames')
        self.assertNotEqual(self.key(changed, 'B'), before['B'])

    def test_a_panel_that_does_reach_the_previous_shot_is_part_of_its_key(self):
        tl = timeline(b_start=21.80)                                                 # on frame 524 (21.8333), which belongs to shot A
        before = self.key(tl, 'A')
        changed = copy.deepcopy(tl)
        changed['shots'][1]['panels'][0]['expo'] = 1.5
        self.assertNotEqual(self.key(changed, 'A'), before)

    def test_key_depends_on_scale_and_on_the_engine_source(self):
        tl = timeline()
        self.assertNotEqual(TL.shot_key(tl, tl['shots'][0], PROJ, 0.5), TL.shot_key(tl, tl['shots'][0], PROJ, 1.0))
        self.assertIn('engine_signature()', open(os.path.join(REPO, 'cutvelvet', 'timeline.py')).read().split('def shot_key')[1].split('\n\n')[0])


class Quality(unittest.TestCase):
    def test_drafts_are_small_and_full_scale_is_untouched(self):
        engine.configure(scale=0.5)
        self.assertEqual(engine.quality(), engine.DRAFT_QUALITY)
        self.assertGreaterEqual(engine.DRAFT_QUALITY[0], 22)
        engine.configure(scale=1.0)
        self.assertEqual(engine.quality(), (18, 'medium'))

    def test_jobs_follow_the_performance_setting(self):
        n = SETTINGS.cores()
        jobs = {m: SETTINGS.mode_jobs(m) for m in ('light', 'balanced', 'fast')}
        self.assertTrue(1 <= jobs['light'] <= jobs['balanced'] <= jobs['fast'] <= n)
        for m, frac in (('light', .25), ('balanced', .5), ('fast', .75)):            # jobs x cores-per-job stays near the intended share
            self.assertLessEqual(jobs[m] * SETTINGS.CORES_PER_JOB, max(SETTINGS.CORES_PER_JOB, n * frac + SETTINGS.CORES_PER_JOB / 2))
        self.assertEqual(SETTINGS.cores_for(1), 1)


@unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg not available')
class Publish(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='cutvelvet-render-'))
        os.makedirs(os.path.join(self.tmp, 'footage')); os.makedirs(os.path.join(self.tmp, 'timelines'))
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc=duration=3:size=640x360:rate=24', '-pix_fmt', 'yuv420p',
                        os.path.join(self.tmp, 'footage', 'a.mp4')], check=True)
        json.dump({'name': 'T', 'canvas': [640, 360], 'fps': 24, 'library': ['footage']}, open(os.path.join(self.tmp, 'project.json'), 'w'))
        tl = {'id': 'demo', 'title': 'Demo', 'duration': 2.0, 'shots': [
            {'id': 'A', 'label': 'a', 't0': 0, 't1': 1.0, 'texts': [], 'fx': {}, 'panels': [dict(panel(0, 1.0), rect=[0, 0, 640, 360])]},
            {'id': 'B', 'label': 'b', 't0': 1.0, 't1': 2.0, 'texts': [], 'fx': {}, 'panels': [dict(panel(1.0, 2.0, ss=1), rect=[0, 0, 640, 360])]}]}
        json.dump(tl, open(os.path.join(self.tmp, 'timelines', 'demo.json'), 'w'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_publish_writes_one_copy_in_review_and_nothing_in_renders(self):
        logs = []
        name = cli.cmd_publish(self.tmp, 'demo', scale=0.5, jobs=1, log=lambda *a: logs.append(' '.join(str(x) for x in a)))
        self.assertEqual(name, 'demo_v1')
        self.assertEqual(sorted(os.listdir(os.path.join(self.tmp, 'review'))), ['demo_v1.mp4', 'demo_v1_shots.json'])   # no hidden leftovers
        renders = os.path.join(self.tmp, 'renders')
        self.assertFalse(os.path.isdir(renders) and os.listdir(renders), 'publish left a second copy in renders/')
        self.assertTrue(os.path.exists(os.path.join(self.tmp, 'timelines', '.history', 'demo_v1.json')))
        self.assertFalse(any('.part' in l for l in logs), logs)
        # re-publishing after changing shot B re-renders only B
        tl = json.load(open(os.path.join(self.tmp, 'timelines', 'demo.json')))
        tl['shots'][1]['panels'][0]['expo'] = 1.4
        json.dump(tl, open(os.path.join(self.tmp, 'timelines', 'demo.json'), 'w'))
        logs.clear()
        self.assertEqual(cli.cmd_publish(self.tmp, 'demo', scale=0.5, jobs=1, log=lambda *a: logs.append(' '.join(str(x) for x in a))), 'demo_v2')
        self.assertIn("re-rendering: ['B']", logs)

    def test_render_without_out_still_uses_renders(self):
        out = cli.render_timeline(self.tmp, 'demo', scale=0.5, jobs=1, log=lambda *a: None)
        self.assertEqual(os.path.dirname(out), os.path.join(self.tmp, 'renders'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
