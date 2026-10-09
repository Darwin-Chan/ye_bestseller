"""Offline browser OS adapter for GUI fixtures that do not test browser launch."""
from types import SimpleNamespace


class FreeBrowserOS:
    def config_error(self, settings):
        if not settings.start_browser:
            return 'GUI 流程要求 browser.start_browser=true；CLI 外部连接不受影响。'
        return None

    def observe(self, settings):
        return SimpleNamespace(state='free', message='测试专用目录未占用')

    def launch(self, settings):
        raise AssertionError('此旧夹具不允许启动真实浏览器')
