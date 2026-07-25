import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        description='K8s Self-Healing AI Framework',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --train      # Train AI models
  python main.py --run        # Start self-healing engine
  python main.py --collect    # Print current pod metrics
        """
    )
    parser.add_argument('--train',   action='store_true', help='Train AI models and save .pkl files')
    parser.add_argument('--run',     action='store_true', help='Start self-healing engine loop')
    parser.add_argument('--collect', action='store_true', help='Collect and print current pod metrics once')
    args = parser.parse_args()

    if args.train:
        from ai.train_models import train_all
        train_all()

    elif args.run:
        from healing.self_healing_engine import SelfHealingEngine
        engine = SelfHealingEngine()
        engine.load_models()
        engine.run()

    elif args.collect:
        from ai.data_collector import collect_all_metrics
        df = collect_all_metrics()
        if df.empty:
            print("No metrics collected. Check: minikube running, Prometheus up, port-forward active.")
        else:
            print("\nCurrent Pod Metrics:")
            print(df.to_string(index=False))

    else:
        parser.print_help()


if __name__ == '__main__':
    main()
