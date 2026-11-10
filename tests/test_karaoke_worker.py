import unittest

from cogs.karaoke_worker import freeze_batch_norm

try:
    import torch
    from torchvision.ops.misc import FrozenBatchNorm2d
except ImportError:
    torch = None


@unittest.skipIf(torch is None, "The optional MDX dependencies are required")
class InferenceNormalizationTests(unittest.TestCase):
    def test_frozen_network_preserves_trained_normalization(self):
        torch.manual_seed(7)
        model = torch.nn.Sequential(
            torch.nn.Conv2d(2, 3, 1),
            torch.nn.BatchNorm2d(3, eps=0.003),
            torch.nn.SiLU(),
            torch.nn.Conv2d(3, 2, 1),
            torch.nn.BatchNorm2d(2, eps=0.001),
        ).eval()
        with torch.no_grad():
            for layer in model.modules():
                if isinstance(layer, torch.nn.BatchNorm2d):
                    layer.weight.copy_(torch.linspace(0.6, 1.7, layer.num_features))
                    layer.bias.copy_(torch.linspace(-0.4, 0.8, layer.num_features))
                    layer.running_mean.copy_(torch.linspace(-0.7, 0.5, layer.num_features))
                    layer.running_var.copy_(torch.linspace(0.3, 1.2, layer.num_features))
            audio_features = torch.randn(2, 2, 5, 7)
            expected = model(audio_features)
            freeze_batch_norm(model)
            actual = model(audio_features)
        torch.testing.assert_close(actual, expected)
        self.assertIsInstance(model[1], FrozenBatchNorm2d)
        self.assertIsInstance(model[4], FrozenBatchNorm2d)


if __name__ == "__main__":
    unittest.main()
