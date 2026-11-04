{
  "description" = "Development shell for the OCIR image cleanup Oracle Function";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      python = pkgs.python312;
    in {
      devShells.${system}.default = pkgs.mkShell {
        packages = [
          python
        ];
      };
    };
}
