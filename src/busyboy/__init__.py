import click


@click.command()
@click.option('--count', default=1, help='Number of greetings.')
def main(count: int) -> None:
    print(f"Hello from busyboy! Count: {count}")
