"""While someone answers, background download rows don't draw over the question."""
import io
import threading
import unittest

from orchard.progress import Clock, Progress
import time


class Tty(io.StringIO):
    def isatty(self):
        return True


class Hold(unittest.TestCase):
    def test_rows_wait_and_this_threads_lines_print(self):
        output = Tty()
        progress = Progress(output)
        task = progress.task('Root')
        with progress.hold():
            thread = threading.Thread(target=lambda: (task.phase('Downloading'), progress.phase('from elsewhere')))
            thread.start()
            thread.join()
            progress.say('Password:')
            self.assertEqual(output.getvalue(), 'Password:\n')
        self.assertIn('from elsewhere', output.getvalue())
        task.phase('Verified')
        self.assertIn('Verified', output.getvalue())

    def test_rows_set_during_a_hold_survive_it(self):
        output = Tty()
        progress = Progress(output)
        task = progress.task('Root')
        with progress.hold():
            task.phase('50%')
            progress.say('Question')
        progress.task('Other').phase('10%')
        self.assertIn('Root', output.getvalue())


class Alongside(unittest.TestCase):
    def test_bars_appear_under_the_title_when_the_hold_ends(self):
        output = Tty()
        progress = Progress(output)
        task = progress.task('Root')
        with progress.hold():
            task.phase('50%')
        self.assertNotIn('Root', output.getvalue())
        progress.section('Getting ready', keep_rows=True)
        self.assertLess(output.getvalue().index('Getting ready'), output.getvalue().index('Root'))

    def test_an_instruction_stays_above_the_block(self):
        output = Tty()
        progress = Progress(output)
        progress.task('Root').phase('50%')
        with progress.alongside('Device'):
            progress.say('Reconnect the cable')
            progress.step('Waiting')
        self.assertIn('  Reconnect the cable\n', output.getvalue())
        self.assertEqual(progress.rows['Device'], 'Waiting')

    def test_device_messages_stay_in_their_row_among_the_bars(self):
        output = Tty()
        progress = Progress(output)
        progress.task('Root').phase('50%')
        with progress.alongside('Device'):
            progress.step('Sending iBSS')
            progress.step('Replug the cable')
            progress.section('Starting')
            progress.transfer(1, 2)
        progress.task('Root').phase('Verified')
        text = output.getvalue()
        self.assertIn('Replug the cable', text)
        self.assertEqual(progress.rows['Root'], 'Verified')
        self.assertEqual(list(progress.rows), ['Root', 'Device'])
        self.assertEqual(progress.rendered, 2)
        # Every redraw moves up over exactly the block, so no line is left behind.
        self.assertNotIn('\033[3A', text)

    def test_without_a_terminal_each_message_is_a_line(self):
        output = io.StringIO()
        progress = Progress(output)
        with progress.alongside('Device'):
            progress.step('Sending iBSS')
        self.assertIn('Sending iBSS', output.getvalue())


class Timing(unittest.TestCase):
    def test_overlapping_input_and_download_count_once(self):
        clock = Clock()
        with clock.exclude('download'):
            with clock.exclude('input'):
                time.sleep(0.05)
        result = clock.result()
        self.assertAlmostEqual(result['elapsed_seconds'] - result['install_seconds'], 0.05, delta=0.03)
        self.assertGreaterEqual(result['excluded_seconds']['download'], 0.05)


if __name__ == '__main__':
    unittest.main()
